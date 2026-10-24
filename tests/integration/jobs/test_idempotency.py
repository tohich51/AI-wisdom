"""C13 — a retried submission starts nothing twice.

``UNIQUE (library_id, idempotency_key)`` is not new; it came with 0001. What
this file checks is the part that is easy to get wrong: the retry path must not
merely avoid a second row, it must leave an attempt that is *already running*
completely alone. A submission that "helpfully" refreshed the row would bump its
xmin and fence out the worker doing the work.
"""

from __future__ import annotations

import pytest
from psycopg import errors

from kb.contracts.enums import ProcessingStatus
from kb.processing import jobs as jobs_domain
from kb.processing import jobs_queue
from kb.processing.jobs import JobKind, JobNotFound
from kb.processing.stages import Stage

pytestmark = pytest.mark.integration


def _submit(conn, library, key: str, kind: JobKind = JobKind.INGEST):
    return jobs_domain.submit_job(conn, library_id=library, kind=kind, idempotency_key=key)


def test_the_first_submission_creates_and_says_so(run_as, cast, library) -> None:
    job, created = run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))

    assert created
    assert job.status is ProcessingStatus.QUEUED
    assert job.library_id == library
    assert job.kind is JobKind.INGEST


def test_the_same_key_resolves_to_the_same_job_and_creates_nothing(
    run_as, cast, library, world
) -> None:
    first, created_first = run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))
    second, created_second = run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert world.job_count(library) == 1


def test_a_retry_does_not_disturb_the_attempt_that_is_already_running(
    run_as, cast, library
) -> None:
    """The important half. A retry must not fence out a live worker."""
    job, _ = run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))
    attempt = run_as(cast.contributor, lambda c: jobs_domain.claim_attempt(c, job.id))

    retried, created = run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))

    assert created is False
    assert retried.id == attempt.id
    # Same token: the retry wrote nothing at all, so the lease still matches.
    assert retried.fence == attempt.fence
    assert retried.status is ProcessingStatus.RUNNING
    # And the holder can still finish. A retry that had bumped xmin would make
    # this commit raise LeaseLost, which is exactly the bug being ruled out.
    finished = run_as(
        cast.contributor,
        lambda c: jobs_domain.commit_status(c, attempt, target=ProcessingStatus.DONE),
    )
    assert finished.status is ProcessingStatus.DONE


def test_the_same_key_in_another_library_is_another_job(run_as, cast, library, world) -> None:
    other = world.library(grants={cast.contributor: "contributor"})
    first, _ = run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))
    second, created = run_as(cast.contributor, lambda c: _submit(c, other, "upload-0001"))

    assert created is True
    assert second.id != first.id
    assert world.job_count(library) == 1
    assert world.job_count(other) == 1


def test_a_stranger_is_refused_by_rls_and_learns_nothing_from_it(run_as, cast, library) -> None:
    """A refusal must not confirm that somebody else's key exists."""
    run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))

    with pytest.raises(errors.InsufficientPrivilege) as refusal:
        run_as(cast.stranger, lambda c: _submit(c, library, "upload-0001"))

    message = str(refusal.value)
    assert "upload-0001" not in message
    assert str(library) not in message


def test_a_key_taken_in_one_library_says_nothing_about_another(run_as, cast, world) -> None:
    """The key is scoped to the library, and that scoping is not a disclosure.

    ``UNIQUE (library_id, idempotency_key)`` includes the library, so the same
    key in two libraries is two jobs and never a conflict. That is the right
    answer for a processing job — a book submitted to two libraries really is
    two pieces of work — and it also means the constraint cannot be used as an
    oracle to ask "does that other library already have this key".
    """
    mine = world.library(grants={cast.contributor: "contributor"})
    theirs = world.library(grants={cast.owner: "manager"})
    theirs_job, _ = run_as(cast.owner, lambda c: _submit(c, theirs, "shared-key-0001"))

    job, created = run_as(cast.contributor, lambda c: _submit(c, mine, "shared-key-0001"))

    assert created is True
    assert job.id != theirs_job.id
    # And the row in the other library is simply not there to be read.

    with pytest.raises(JobNotFound):
        run_as(cast.contributor, lambda c: jobs_domain.read_job(c, theirs_job.id))


def test_a_stranger_may_not_start_processing_at_all(run_as, cast, library) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        run_as(cast.stranger, lambda c: _submit(c, library, "upload-0001"))


def test_a_worker_cannot_create_a_job(run_as, worker_identity, library, worker_pool) -> None:
    """``kb_worker`` holds SELECT and UPDATE on kb.job, and no INSERT.

    A worker that could create its own work could claim a job for a library
    nobody asked it to touch, and the ACL would never see a submission.
    """
    with pytest.raises(errors.InsufficientPrivilege):
        run_as(
            worker_identity.principal_id,
            lambda c: _submit(c, library, "worker-made-up-01"),
            pool=worker_pool,
        )


# ------------------------------------------------------------- the queue side


def test_a_stage_cannot_be_queued_twice_while_it_is_waiting(
    queue_app, run_as, cast, library, world
) -> None:
    job, _ = run_as(cast.contributor, lambda c: _submit(c, library, "upload-0001"))

    first = run_as(
        cast.contributor,
        lambda c: jobs_queue.enqueue_stages(queue_app, c, job_id=job.id, stages=(Stage.PARSE,)),
    )
    assert len(first) == 1

    with pytest.raises(jobs_queue.TaskAlreadyEnqueued):
        run_as(
            cast.contributor,
            lambda c: jobs_queue.enqueue_stages(queue_app, c, job_id=job.id, stages=(Stage.PARSE,)),
        )
    # The refusal is a refusal: exactly one row, still waiting.
    rows = world.queue_rows(job.id)
    assert len(rows) == 1
    assert rows[0][0] == jobs_queue.stage_task_name(Stage.PARSE)
    assert rows[0][1] == "todo"


class _Rollback(Exception):
    pass


def test_the_queue_row_and_the_job_row_commit_together(
    queue_app, bound, cast, library, world
) -> None:
    """A rolled-back submission leaves no queue row, and no job row.

    This is why the enqueue runs on the caller's connection rather than on one
    of the queue's own: there is no window in which the catalogue knows about a
    job the queue has never heard of, or the other way round.
    """
    job_id = None
    with pytest.raises(_Rollback):
        with bound(cast.contributor) as conn:
            job, _ = _submit(conn, library, "rolled-back-01")
            job_id = job.id
            jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.PARSE,))
            raise _Rollback

    assert job_id is not None
    assert world.job_count(library) == 0
    assert world.queue_rows(job_id) == []


def test_a_committed_submission_leaves_exactly_one_job_and_one_queue_row(
    queue_app, bound, cast, library, world
) -> None:
    with bound(cast.contributor) as conn:
        job, _ = _submit(conn, library, "committed-01")
        jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.PARSE, Stage.EMBED))

    assert world.job_count(library) == 1
    rows = world.queue_rows(job.id)
    assert [row[0] for row in rows] == [
        jobs_queue.stage_task_name(Stage.PARSE),
        jobs_queue.stage_task_name(Stage.EMBED),
    ]


def test_enqueue_reaches_a_real_procrastinate_table_not_a_stand_in(queue_app, world) -> None:
    """If this ever stops being the library's own table, the queue is a mock."""
    row = world.conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_name = 'procrastinate_jobs'"
    ).fetchone()
    assert row is not None, "procrastinate_jobs does not exist; the queue is not real"
    owner = world.conn.execute(
        "SELECT tableowner FROM pg_tables WHERE tablename = 'procrastinate_jobs'"
    ).fetchone()
    assert owner is not None
    del owner, queue_app
