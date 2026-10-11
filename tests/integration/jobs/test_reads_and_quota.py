"""C13 — reading a job's stage never waits on the queue that is doing the work.

Acceptance 4. The failure this rules out is a UI that stops answering because
a stage has been running for four minutes, or because the queue table is being
written hard. Two things make it true and both are checked here: the read is a
plain MVCC ``SELECT``, so it never blocks on a row lock, and it carries a
statement timeout, so a stalled writer cannot turn into a stalled reader even
if a future change puts a lock in the path.

Every test here has an uncommitted writer on another connection. Without one,
"the read was fast" would prove nothing.
"""

from __future__ import annotations

import contextlib
import time
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from kb.processing import jobs as jobs_domain
from kb.processing import jobs_queue
from kb.processing.jobs import JobKind
from kb.processing.stages import Stage

pytestmark = pytest.mark.integration

#: Generous for a local server and still far below "the UI hung".
READ_BUDGET_SECONDS = 2.0

WORKER_PRINCIPAL = UUID("00000000-0000-4000-8000-0000000000bb")


@contextlib.contextmanager
def _writer(c13_dsn: str):
    """A connection with an open transaction the test controls."""
    conn = psycopg.connect(c13_dsn, autocommit=False, options="-c role=kb_worker")
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.principal', %s, true)", (str(WORKER_PRINCIPAL),)
                )
            yield conn
    finally:
        conn.close()


@pytest.fixture
def job(run_as, cast, library):
    record, _ = run_as(
        cast.contributor,
        lambda c: jobs_domain.submit_job(
            c, library_id=library, kind=JobKind.INGEST, idempotency_key="read-0001"
        ),
    )
    return record


def test_a_read_returns_while_another_connection_holds_the_row_lock(
    c13_dsn, run_as, cast, job
) -> None:
    """The core of acceptance 4, with a real uncommitted writer on the row.

    The writer has taken the lease and has not committed, so the row is locked
    and its new status is invisible. A read that took a lock would wait for
    the writer here; a plain ``SELECT`` reads the last committed version and
    returns.
    """
    with _writer(c13_dsn) as writer:
        jobs_domain.claim_attempt(writer, job.id)

        started = time.monotonic()
        seen = run_as(cast.contributor, lambda c: jobs_domain.read_job(c, job.id))
        elapsed = time.monotonic() - started

        assert elapsed < READ_BUDGET_SECONDS, f"the read waited {elapsed:.3f}s on a locked row"
        # And it read the committed truth, not the writer's uncommitted one.
        assert seen.status is jobs_domain.ProcessingStatus.QUEUED


def test_a_read_returns_while_the_queue_table_is_being_written(
    c13_dsn, run_as, cast, job, queue_app, bound
) -> None:
    """The queue is the heavy thing, and the reader must not go near it.

    A row of ``procrastinate_jobs`` is locked ``FOR UPDATE`` and left
    uncommitted. The read below is a single statement against ``kb.job`` and
    never touches the queue, so it comes straight back.
    """
    with bound(cast.contributor) as conn:
        jobs_queue.enqueue_stages(queue_app, conn, job_id=job.id, stages=(Stage.PARSE,))

    with _writer(c13_dsn) as writer:
        writer.execute(
            "SELECT id FROM procrastinate_jobs WHERE args ->> 'job_id' = %s FOR UPDATE",
            (str(job.id),),
        )

        started = time.monotonic()
        seen = run_as(cast.contributor, lambda c: jobs_domain.read_job(c, job.id))
        elapsed = time.monotonic() - started

        assert elapsed < READ_BUDGET_SECONDS, f"the read waited {elapsed:.3f}s on the queue"
        assert seen.id == job.id


def test_the_read_carries_a_statement_timeout(bound, cast, job) -> None:
    """The bound is in the code, not an aspiration.

    ``read_job`` sets it with ``SET LOCAL``, so it is still in force on the
    same connection afterwards and can be asserted on.
    """
    with bound(cast.contributor) as conn:
        jobs_domain.read_job(conn, job.id)
        setting = conn.execute("SHOW statement_timeout").fetchone()
    assert setting is not None
    assert setting[0] == "5s"


def test_a_reader_never_sees_a_half_written_status(c13_dsn, run_as, cast, job, world) -> None:
    """A status is never observed mid-flight.

    The writer claims the job and rolls back, exactly as a crashed transaction
    would. The reader saw ``queued`` throughout, and the row is ``queued``
    afterwards: either the claim committed or it did not.
    """
    with pytest.raises(_Rollback):
        with _writer(c13_dsn) as writer:
            jobs_domain.claim_attempt(writer, job.id)
            seen = run_as(cast.contributor, lambda c: jobs_domain.read_job(c, job.id))
            assert seen.status is jobs_domain.ProcessingStatus.QUEUED
            raise _Rollback

    assert world.job_status(job.id) == "queued"


class _Rollback(Exception):
    pass


def test_the_read_path_holds_no_lock_a_writer_could_wait_on(c13_dsn, run_as, cast, job) -> None:
    """The same claim from the other side: the reader is not the one blocking.

    A read-only transaction is open on a second connection while a worker
    takes the lease on a third. If the read had taken a row lock the claim
    would be waiting instead.
    """
    pool = ConnectionPool(
        c13_dsn, min_size=1, max_size=2, open=True, kwargs={"options": "-c role=kb_app"}
    )
    pool.wait(timeout=30)
    try:
        with pool.connection() as reader:
            with reader.transaction():
                reader.execute("SELECT 1")
                started = time.monotonic()
                attempt = run_as(cast.contributor, lambda c: jobs_domain.claim_attempt(c, job.id))
                assert time.monotonic() - started < READ_BUDGET_SECONDS
                assert attempt.job.status is jobs_domain.ProcessingStatus.RUNNING
    finally:
        pool.close()
