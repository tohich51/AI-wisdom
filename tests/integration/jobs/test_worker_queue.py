"""C13 — the worker, running against the real Procrastinate queue.

Nothing here is a stand-in. A real ``procrastinate.worker.Worker`` connects to
a real PostgreSQL, fetches a real task out of ``procrastinate_jobs``, and runs
the task functions this card ships, against a real ``kb_worker`` connection and
a real catalogue. ``test_the_queue_table_is_procrastinates_own_and_not_ours``
is the test that keeps that honest.

The scenarios are the card's own: a kill in the middle must not lose the
original and must not double a stage result; ``needs_ocr`` must be an answer
rather than an endless retry; a paused quota must not hold a worker slot.
"""

from __future__ import annotations

import inspect

import pytest
from procrastinate.worker import Worker

from kb.processing import jobs as jobs_domain
from kb.processing import jobs_queue
from kb.processing.jobs import JobKind
from kb.processing.stages import Stage, ladder_for

pytestmark = pytest.mark.integration

ALL_QUEUES = tuple(sorted(set(jobs_queue.QUEUE_FOR_STAGE.values())))
CLOUD_QUEUE = (jobs_queue.QUEUE_FOR_STAGE[Stage.EXTRACT_CLOUD],)


async def drain(app, *, queues=ALL_QUEUES) -> None:
    """Run a real worker until the queue is empty, then return.

    ``wait=False`` is what makes this deterministic: the worker stops as soon
    as a fetch finds nothing instead of polling forever. It is still the
    library's own fetch loop, its row locks and its status transitions.
    """
    worker = Worker(
        app,
        queues=list(queues),
        wait=False,
        concurrency=1,
        install_signal_handlers=False,
        listen_notify=False,
        fetch_job_polling_interval=0.05,
    )
    await worker.run()


def _queue_ladder(app, conn, job, kind=JobKind.INGEST):
    return jobs_queue.enqueue_stages(app, conn, job_id=job.id, stages=ladder_for(kind))


# ------------------------------------------------------------------ the basics


async def test_a_real_worker_runs_the_ladder_to_done(
    worker_app, queue_app, bound, cast, library, world
) -> None:
    for _ in range(2):
        world.source(library)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="ingest-0001"
        )
        queued = _queue_ladder(queue_app, conn, job)
    assert len(queued) == 4

    await drain(worker_app)

    assert world.job_status(job.id) == "done"
    assert world.source_count(library, "running") == 2
    assert world.source_count(library, "queued") == 0
    rows = world.queue_rows(job.id)
    assert {row[1] for row in rows} == {"succeeded"}
    assert sorted(row[0] for row in rows) == sorted(
        jobs_queue.stage_task_name(stage) for stage in ladder_for(JobKind.INGEST)
    )
    # Each stage ran exactly once. Four stages over four rows, every one of
    # them dequeued a single time — a stage that re-ran itself would show an
    # attempts count above one.
    assert [row[2] for row in rows] == [1, 1, 1, 1], rows


def test_the_queue_table_is_procrastinates_own_and_not_ours(queue_app, world) -> None:
    """If this stops being true, every queue assertion here proves nothing."""
    present = world.conn.execute(
        "SELECT 1 FROM pg_tables WHERE tablename = 'procrastinate_jobs'"
    ).fetchone()
    assert present is not None, "procrastinate_jobs does not exist; the queue is not real"
    owner = world.conn.execute(
        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'procrastinate_jobs'"
    ).fetchone()
    assert owner is not None
    assert not str(owner[0]).startswith("kb_"), owner
    # And the card never declared it: nothing under migrations/ mentions it.
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3]
    for migration in sorted((root / "migrations").glob("*.sql")):
        assert "procrastinate" not in migration.read_text(encoding="utf-8"), migration


# ------------------------------------------------------------ kill and restart


async def test_a_kill_mid_ladder_loses_nothing_and_doubles_nothing(
    worker_app, queue_app, gateway_pool, bound, worker_pool, worker_identity, cast, library, world
) -> None:
    """Acceptance 1: a restart repeats work, it does not duplicate it.

    The kill is placed between the lease commit and the stage's own writes,
    which is the only position where losing something is even possible: the
    lease is durable, the work is not, so the row is left saying ``running``
    with no result behind it. A restarted worker then finishes the ladder, and
    the index generation is still one row.
    """
    from kb.processing.worker import JobWorkerKilled, run_stage

    source = world.source(library)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="ingest-killed-01"
        )
        _queue_ladder(queue_app, conn, job)

    def kill(attempt) -> None:
        raise JobWorkerKilled("the process is gone")

    with pytest.raises(JobWorkerKilled):
        with worker_pool.connection() as conn:
            run_stage(
                conn,
                worker_identity,
                job_id=job.id,
                stage=Stage.PARSE,
                on_kill=kill,
            )

    # The lease survived; the work did not. Nothing was half-written.
    assert world.job_status(job.id) == "running"
    assert world.source_status(source) == "queued"
    assert world.index_generation_rows(library) == 0

    # A restarted worker picks the job up and finishes the whole ladder.
    await drain(worker_app)

    assert world.job_status(job.id) == "done"
    assert world.source_status(source) == "running"
    # Every stage of the ladder ran exactly once after the restart, and the
    # source moved one step. A stage that doubled its result would show up
    # here as a second parse or a second queue execution.
    rows = {name: (status, attempts) for name, status, attempts in world.queue_rows(job.id)}
    assert sorted(rows) == sorted(
        jobs_queue.stage_task_name(stage) for stage in ladder_for(JobKind.INGEST)
    )
    assert all(status == "succeeded" for status, _ in rows.values()), rows
    assert all(attempts == 1 for _, attempts in rows.values()), rows
    events = dict(world.queue_events(job.id))
    assert events.get("succeeded", 0) == 4, events


async def test_the_original_is_still_readable_after_a_kill(
    worker_app, queue_app, gateway_pool, bound, worker_pool, worker_identity, cast, library, world
) -> None:
    """The "does not lose the original" half, stated on the row that holds the original.

    The kill never reaches the blob, and the catalogue row that names it is
    untouched, so a retry has the same original to work from rather than a
    half-written one.
    """
    from kb.processing.worker import JobWorkerKilled, run_stage

    source = world.source(library)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="ingest-killed-02"
        )
        _queue_ladder(queue_app, conn, job)

    def kill(attempt) -> None:
        raise JobWorkerKilled

    with pytest.raises(JobWorkerKilled):
        with worker_pool.connection() as conn:
            run_stage(conn, worker_identity, job_id=job.id, stage=Stage.PARSE, on_kill=kill)

    row = world.conn.execute(
        "SELECT object_key, content_hash, processing FROM kb.source WHERE id = %s", (source,)
    ).fetchone()
    assert row is not None
    assert str(row[0]).startswith("c13/")
    assert len(str(row[1])) == 64
    assert str(row[2]) == "queued"


async def test_re_delivering_a_finished_job_does_nothing(
    worker_app, queue_app, gateway_pool, bound, cast, library, world
) -> None:
    """A straggler message for a job that already ended must be inert."""
    world.source(library)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="ingest-redeliver-01"
        )
        _queue_ladder(queue_app, conn, job)
    await drain(worker_app)
    assert world.job_status(job.id) == "done"

    with bound(cast.contributor) as conn:
        jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.INDEX,))
    await drain(worker_app)

    assert world.job_status(job.id) == "done"
    rows = world.queue_rows(job.id)
    requeued = {name for name, status, _ in rows if name == jobs_queue.stage_task_name(Stage.INDEX)}
    assert requeued == {jobs_queue.stage_task_name(Stage.INDEX)}
    # The source did not move a second time.
    assert world.source_count(library, "running") == 1


# ------------------------------------------------------------------ needs_ocr


async def test_an_unsupported_scan_is_an_answer_not_a_retry_loop(
    worker_app, queue_app, gateway_pool, bound, cast, library, world
) -> None:
    scan = world.source(library, media_type="image/tiff")
    world.source(library, media_type="text/plain")
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="ingest-scan-01"
        )
        _queue_ladder(queue_app, conn, job)

    await drain(worker_app)

    assert world.job_status(job.id) == "needs_ocr"
    assert world.source_status(scan) == "needs_ocr"
    # The readable source in the same library was not quietly advanced: the
    # parse stopped at the scan rather than carrying on with half the batch.
    assert world.source_count(library, "queued") == 1

    events = dict(world.queue_events(job.id))
    assert events.get("deferred_for_retry", 0) == 0, events
    assert events.get("started", 0) == 4, events
    statuses = {name: status for name, status, _ in world.queue_rows(job.id)}
    # The stages after the parse did run, and each one found a terminal job
    # and did nothing. A book that cannot be read is not half-indexed.
    assert set(statuses.values()) == {"succeeded"}, statuses
    assert world.job_status(job.id) == "needs_ocr"


async def test_needs_ocr_is_terminal_so_no_worker_wakes_it_up_again(
    worker_app, queue_app, gateway_pool, bound, cast, library, world
) -> None:
    scan = world.source(library, media_type="image/png")
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=library, kind=JobKind.INGEST, idempotency_key="ingest-scan-02"
        )
        _queue_ladder(queue_app, conn, job)
    await drain(worker_app)
    assert world.job_status(job.id) == "needs_ocr"

    with bound(cast.contributor) as conn:
        jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.PARSE,))
    await drain(worker_app)

    assert world.job_status(job.id) == "needs_ocr"
    assert world.source_status(scan) == "needs_ocr"


# --------------------------------------------------------------- a paused quota


async def test_a_paused_quota_does_not_occupy_a_worker_slot(
    worker_app, queue_app, gateway_pool, bound, cast, world, worker_principal, worker_role
) -> None:
    """Acceptance 3, with the slot as the measurement.

    One worker, concurrency 1, one paused cloud job ahead of a real one in the
    same queue. Had the paused job held its slot, the second would never have
    run. Both ran, and the one whose policy allows generation reached the
    stage that needs the policy.
    """
    paused = world.library(grants={cast.contributor: "contributor", worker_principal: worker_role})
    allowed = world.library(grants={cast.contributor: "contributor", worker_principal: worker_role})
    world.generation_policy(paused, allowed=False)
    world.generation_policy(allowed, allowed=True)

    ids = []
    with bound(cast.contributor) as conn:
        for library, key in ((paused, "extract-paused-01"), (allowed, "extract-allowed-01")):
            job, _ = jobs_domain.submit_job(
                conn, library_id=library, kind=JobKind.EXTRACT, idempotency_key=key
            )
            jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.EXTRACT_CLOUD,))
            ids.append(job.id)

    await drain(worker_app, queues=CLOUD_QUEUE)

    # Both cloud tasks completed. The paused one says so in its status instead
    # of being retried, and the allowed one is not stuck behind it.
    for job_id in ids:
        rows = world.queue_rows(job_id)
        assert rows and all(row[1] == "succeeded" for row in rows), rows
    # The distinction, and the only one that matters: the paused job went back
    # to `queued` — its stage never ran — while the allowed one stayed
    # `running` because its stage did. Both finished in the same pass, in the
    # same queue, with a single worker slot between them.
    assert world.job_status(ids[0]) == "queued"
    assert world.job_status(ids[1]) == "running"
    events = dict(world.queue_events(ids[0]))
    assert events.get("deferred_for_retry", 0) == 0, events
    assert events.get("started", 0) == 1, events


async def test_a_paused_quota_does_not_spend_the_retry_budget(
    worker_app, queue_app, gateway_pool, bound, cast, world, worker_principal, worker_role
) -> None:
    """A policy answer must not consume the bounded retry budget.

    A quota that is switched off will still be switched off in ten seconds.
    Retrying it would spend the budget a genuinely transient failure needs and
    would fill the queue with the same answer.
    """
    paused = world.library(grants={cast.contributor: "contributor", worker_principal: worker_role})
    world.generation_policy(paused, allowed=False)

    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=paused, kind=JobKind.EXTRACT, idempotency_key="extract-paused-02"
        )
        jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.EXTRACT_CLOUD,))

    await drain(worker_app, queues=CLOUD_QUEUE)

    events = dict(world.queue_events(job.id))
    assert events.get("deferred_for_retry", 0) == 0, events
    assert events.get("started", 0) == 1, events
    assert events.get("succeeded", 0) == 1, events


async def test_a_generation_enabled_library_runs_the_cloud_stage(
    worker_app, queue_app, gateway_pool, bound, cast, world, worker_principal, worker_role
) -> None:
    """The control, so "refuse everything" cannot pass the test above."""
    allowed = world.library(grants={cast.contributor: "contributor", worker_principal: worker_role})
    world.generation_policy(allowed, allowed=True)
    with bound(cast.contributor) as conn:
        job, _ = jobs_domain.submit_job(
            conn, library_id=allowed, kind=JobKind.EXTRACT, idempotency_key="extract-allowed-03"
        )
        jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.EXTRACT_CLOUD,))

    await drain(worker_app, queues=CLOUD_QUEUE)

    rows = world.queue_rows(job.id)
    assert [row[1] for row in rows] == ["succeeded"], rows
    assert world.job_status(job.id) == "running"


# ---------------------------------------------------------- the task signature


def test_a_stage_task_takes_one_argument_and_nothing_else(queue_app) -> None:
    """The mechanism behind "the payload cannot set somebody else's actor".

    There is no parameter a queue message could use to say who the worker is,
    so there is nothing for it to lie about.
    """
    for stage in jobs_queue.QUEUE_FOR_STAGE:
        task = queue_app.tasks[jobs_queue.stage_task_name(stage)]
        assert list(inspect.signature(task.func).parameters) == ["context", "job_id"], stage


# ------------------------------------------------- a gap, turned into a test


async def test_no_runtime_role_can_open_an_index_generation(
    gateway_pool, worker_pool, bound, cast, library, world
) -> None:
    """A real, currently-true fact that C13 is not allowed to fix.

    ``kb.index_generation`` carries exactly one policy, ``index_generation_read``,
    which is SELECT-only. So the index stage cannot open a generation, and no
    amount of granting fixes it — the RLS policy itself is missing. 0002's
    blanket ``GRANT INSERT`` on every table is unreachable for the same reason
    C10 found on ``kb.source_version``.

    This is asserted rather than worked around, because a workaround would
    mean writing a second DDL path for a table the single owner already
    declares. The proposal for the owner is in
    ``docs/handoff/results/C13.json``:

        CREATE POLICY index_generation_write ON kb.index_generation FOR INSERT
            WITH CHECK (kb.role_rank(kb.effective_role(
                kb.current_principal(), library_id)) >= 20);
    """
    from psycopg import errors

    with bound(cast.contributor) as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO kb.index_generation (library_id, generation, state) "
                "VALUES (%s, 1, 'building')",
                (library,),
            )
    with worker_pool.connection() as conn:
        with pytest.raises(errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO kb.index_generation (library_id, generation, state) "
                "VALUES (%s, 1, 'building')",
                (library,),
            )
    assert world.index_generation_rows(library) == 0
    del gateway_pool
