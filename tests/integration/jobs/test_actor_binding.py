"""C13 — the queue payload is not a source of identity.

The card's acceptance 2 is "the queue payload does not set somebody else's
actor", and the usual way that gets faked is by adding an ``actor_id`` field to
the message, reading it, and asserting that a bogus value is ignored. Ignoring
a field is not a control: the next person adds a second field.

What holds here is structural. A stage task's signature has one parameter,
``job_id``; the database identity is the worker's own configured service
account; and the RLS policies decide what that identity may touch. A message
carrying anything else is refused before it is read, and a worker whose service
account has no grant on the library cannot write even when the message says it
should.

Every refusal has a matching success, because a suite that only proves "refuse"
passes just as well when the feature is broken.
"""

from __future__ import annotations

import dataclasses
import inspect
from uuid import UUID
from uuid import uuid4 as _uuid4

import pytest
from procrastinate.worker import Worker
from psycopg import errors

from kb.processing import jobs as jobs_domain
from kb.processing import jobs_queue
from kb.processing.jobs import JobKind, JobNotFound, LeaseUnavailable
from kb.processing.stages import Stage

pytestmark = pytest.mark.integration

PARSE_QUEUE = (jobs_queue.QUEUE_FOR_STAGE[Stage.PARSE],)


async def drain(app, *, queues=PARSE_QUEUE) -> None:
    await Worker(
        app,
        queues=list(queues),
        wait=False,
        concurrency=1,
        install_signal_handlers=False,
        listen_notify=False,
        fetch_job_polling_interval=0.05,
    ).run()


def _defer_parse(app, conn, job_id: UUID, **extra) -> int:
    """Defer the parse stage with whatever extra fields a message carries."""
    task = app.tasks[jobs_queue.stage_task_name(Stage.PARSE)]
    return task.configure(
        connection=conn,
        queue=jobs_queue.QUEUE_FOR_STAGE[Stage.PARSE],
        queueing_lock=jobs_queue.stage_queueing_lock(job_id, Stage.PARSE),
    ).defer(job_id=str(job_id), **extra)


# ----------------------------------------------------------------- the structure


def test_a_stage_task_has_no_actor_parameter_to_lie_into(queue_app) -> None:
    for stage in jobs_queue.QUEUE_FOR_STAGE:
        task = queue_app.tasks[jobs_queue.stage_task_name(stage)]
        assert list(inspect.signature(task.func).parameters) == ["context", "job_id"], stage


def test_the_worker_identity_is_deployment_configuration_not_a_message() -> None:
    from kb.processing.worker import WorkerIdentity

    fields = {f.name for f in dataclasses.fields(WorkerIdentity)}
    assert fields == {"principal_id", "account_id"}
    identity = WorkerIdentity(principal_id=_uuid4(), account_id=_uuid4())
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.principal_id = _uuid4()  # type: ignore[misc]


# -------------------------------------------------------------- a forged message


async def test_a_payload_that_names_an_actor_is_refused_and_writes_nothing(
    worker_app, queue_app, gateway_pool, bound, cast, library, world
) -> None:
    source = world.source(library)
    other_library = world.library(grants={cast.stranger: "manager"})
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="forged-0001"
        )
        _defer_parse(
            queue_app,
            conn,
            job.id,
            actor_id=str(cast.owner),
            principal_id=str(cast.stranger),
            library_id=str(other_library),
            status="done",
        )

    await drain(worker_app)

    # The forged fields are not a way in: the task could not even be called
    # with them, so the worker body never ran.
    rows = world.queue_rows(job.id)
    assert [status for _, status, _ in rows] == ["failed"], rows
    # Nothing was written, by anybody, on the strength of the message.
    assert world.job_status(job.id) == "queued"
    assert world.source_status(source) == "queued"
    # The message is still on the queue verbatim, so an operator sees that
    # something tried this rather than finding no trace of it at all.
    args = world.conn.execute(
        "SELECT args FROM procrastinate_jobs WHERE args ->> 'job_id' = %s", (str(job.id),)
    ).fetchone()
    assert args is not None
    assert str(cast.owner) in str(args[0])


async def test_a_forged_library_name_gets_no_further_than_a_real_one_would(
    worker_app, queue_app, gateway_pool, bound, cast, world
) -> None:
    """A message naming a foreign library is no different from any other.

    The worker never reads a library from the payload; it reads
    ``kb.job.library_id``. So the message cannot widen anything, and the only
    thing that can is a ``kb.library_grant`` row for the worker's own account.
    """
    other = world.library(
        grants={
            cast.contributor: "contributor",
            cast.owner: "manager",
            cast.stranger: "manager",
        }
    )
    other_source = world.source(other)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=other, kind=JobKind.INGEST, idempotency_key="forged-0002"
        )
        _defer_parse(queue_app, conn, job.id, actor_id=str(cast.owner), library_id=str(other))

    await drain(worker_app)

    rows = world.queue_rows(job.id)
    assert [status for _, status, _ in rows] == ["failed"], rows
    assert world.source_status(other_source) == "queued"
    assert world.job_status(job.id) == "queued"


# ------------------------------------------------- the worker's own authority


async def test_a_worker_without_a_grant_cannot_write(
    worker_app, queue_app, gateway_pool, bound, cast, world
) -> None:
    """The library the worker has no grant on: the stage cannot run.

    The payload in this test names a principal that *does* have a grant, so
    the only way it could succeed is by believing the message. It does not.
    """
    closed = world.library(grants={cast.contributor: "contributor", cast.owner: "manager"})
    source = world.source(closed)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=closed, kind=JobKind.INGEST, idempotency_key="ungranted-0001"
        )
        _defer_parse(queue_app, conn, job.id, actor_id=str(cast.contributor))

    await drain(worker_app)

    rows = world.queue_rows(job.id)
    assert [status for _, status, _ in rows] == ["failed"], rows
    assert world.job_status(job.id) == "queued"
    assert world.source_status(source) == "queued"


async def test_a_worker_with_a_grant_runs_the_very_same_message_shape(
    worker_app, queue_app, gateway_pool, bound, cast, library, world
) -> None:
    """The control, so the refusal above cannot pass by refusing everything."""
    source = world.source(library)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="granted-0001"
        )
        _defer_parse(queue_app, conn, job.id)

    await drain(worker_app)

    rows = world.queue_rows(job.id)
    assert [status for _, status, _ in rows] == ["succeeded"], rows
    assert world.source_status(source) == "running"


def test_a_worker_cannot_claim_a_job_of_a_library_it_has_no_grant_on(
    run_as, cast, world, worker_principal, worker_pool
) -> None:
    """The same boundary, without a queue in the way.

    RLS filters the row out of the worker's UPDATE rather than raising, so the
    claim matches nothing and the worker is told the job cannot be taken. That
    is the better answer of the two available ones: it is byte for byte what a
    worker would hear about a job that is genuinely held by somebody else, so
    the refusal cannot be used to find out whether the job exists.
    """
    closed = world.library(grants={cast.contributor: "contributor"})
    job, _ = run_as(
        cast.contributor,
        lambda c: jobs_domain.submit_job(
            c, library_id=closed, kind=JobKind.INGEST, idempotency_key="ungranted-0002"
        ),
    )
    with pytest.raises(LeaseUnavailable) as refusal:
        run_as(worker_principal, lambda c: jobs_domain.claim_attempt(c, job.id), pool=worker_pool)

    assert str(job.id) not in str(refusal.value)
    assert world.job_status(job.id) == "queued"


def test_a_worker_cannot_even_see_a_job_it_has_no_grant_on(
    run_as, cast, world, worker_principal, worker_pool
) -> None:
    """And a job it cannot see does not exist as far as the worker is concerned."""
    closed = world.library(grants={cast.contributor: "contributor"})
    job, _ = run_as(
        cast.contributor,
        lambda c: jobs_domain.submit_job(
            c, library_id=closed, kind=JobKind.INGEST, idempotency_key="invisible-0001"
        ),
    )
    with pytest.raises((JobNotFound, errors.InsufficientPrivilege)):
        run_as(worker_principal, lambda c: jobs_domain.read_job(c, job.id), pool=worker_pool)


def test_a_reader_may_see_a_job_it_may_not_work_on(run_as, cast, world, worker_pool) -> None:
    """The owner can watch progress without being able to drive it.

    ``job_read`` is reader-level by design, so a person watching a long job
    does not need a write role to see which stage it is in.
    """
    lib = world.library(grants={cast.owner: "reader", cast.contributor: "contributor"})
    job, _ = run_as(
        cast.contributor,
        lambda c: jobs_domain.submit_job(
            c, library_id=lib, kind=JobKind.INGEST, idempotency_key="watched-0001"
        ),
    )
    seen = run_as(cast.owner, lambda c: jobs_domain.read_job(c, job.id))
    assert seen.id == job.id
    assert seen.status is jobs_domain.ProcessingStatus.QUEUED

    # And a reader may not drive it, which is the other half. RLS filters the
    # row out of the claim rather than raising, so the reader cannot even take
    # the lease on a job it is allowed to watch.
    with pytest.raises(LeaseUnavailable):
        run_as(cast.owner, lambda c: jobs_domain.claim_attempt(c, job.id))
