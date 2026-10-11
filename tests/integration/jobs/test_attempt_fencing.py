"""C13 — attempt fencing, against two connections that really race.

A fencing test that calls one function after another proves nothing: the loser
is whoever returned second, and the code under test was never under pressure.
Every race in this file is arranged so that PostgreSQL is *observed* blocking —
the suite polls ``pg_stat_activity`` from a third connection until it sees the
second backend sitting in ``wait_event_type = 'Lock'`` — and only then lets the
first one go. If the block did not happen the poll times out and the test
fails. It never degrades quietly into a sequential call.

The arrangement that matters is the card's central claim: a worker that has
lost its lease must not be able to write a result, and it must be rejected even
when it wakes up late. So one connection takes the lease and stalls, a second
takes it over, and the first then tries to finish.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from kb.access.policy import Principal, transaction_identity
from kb.contracts.enums import ProcessingStatus
from kb.processing import jobs as jobs_domain
from kb.processing.jobs import JobKind, LeaseLost, LeaseUnavailable

pytestmark = pytest.mark.integration

#: Long enough that a real lock wait is never mistaken for a fast failure,
#: short enough that a mistake does not hang the suite.
BLOCK_POLL_SECONDS = 10.0
BLOCK_POLL_INTERVAL = 0.02


def _conn(dsn: str) -> psycopg.Connection:
    """One connection as the worker role. Never as the superuser.

    A superuser BYPASSES RLS, which would make every assertion here vacuous.
    """
    return psycopg.connect(dsn, autocommit=False, options="-c role=kb_worker")


def _worker_pool(dsn: str) -> ConnectionPool:
    pool = ConnectionPool(
        dsn, min_size=1, max_size=4, open=True, kwargs={"options": "-c role=kb_worker"}
    )
    pool.wait(timeout=30)
    return pool


def _principal(identity) -> Principal:
    return Principal(
        principal_id=identity.principal_id,
        account_id=identity.account_id,
        generation_watermark=1,
    )


def _await_block(admin: psycopg.Connection, pid: int) -> None:
    """Block until the server says this backend is waiting on a lock.

    This is the difference between a race and a rehearsal. If the second
    connection is never observed waiting, the two statements did not overlap,
    nothing was proved, and the test fails rather than passing quietly.
    """
    deadline = time.monotonic() + BLOCK_POLL_SECONDS
    while time.monotonic() < deadline:
        row = admin.execute(
            "SELECT state, wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        if row is not None and row[0] == "active" and row[1] == "Lock":
            return
        time.sleep(BLOCK_POLL_INTERVAL)
    raise AssertionError(
        f"backend {pid} was never observed waiting on a lock; the statements did not race"
    )


class Racer:
    """Runs one statement on its own connection, in its own thread.

    The connection is created inside the thread, so nothing is shared with the
    test. The backend pid is handed back before the statement starts, which is
    what makes it possible to watch the server park that backend on a lock.
    """

    def __init__(self, dsn: str, body: Callable[[psycopg.Connection], Any]) -> None:
        self._dsn = dsn
        self._body = body
        self._pids: queue.Queue[int] = queue.Queue()
        self.result: Any = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> int:
        self._thread.start()
        return self._pids.get(timeout=BLOCK_POLL_SECONDS)

    def _run(self) -> None:
        conn = _conn(self._dsn)
        try:
            self._pids.put(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
            self.result = self._body(conn)
        except BaseException as exc:
            self.error = exc
        finally:
            conn.close()

    def join(self, timeout: float = BLOCK_POLL_SECONDS) -> None:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the racing connection never finished"

    def expect(self, kind: type[BaseException]) -> BaseException:
        self.join()
        assert self.error is not None, f"expected {kind.__name__}; the call succeeded"
        assert isinstance(self.error, kind), f"expected {kind.__name__}, got {self.error!r}"
        return self.error


@contextlib.contextmanager
def _holding(conn: psycopg.Connection, identity):
    """An explicit transaction the test controls, so it commits at a chosen
    moment while a second connection is parked on the row lock.

    ``transaction_identity`` is not usable here: it commits on exit, and these
    tests need to decide for themselves *when* the holder lets go.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.principal', %s, true)", (str(identity.principal_id),)
            )
            cur.execute("SELECT set_config('app.account', %s, true)", (str(identity.account_id),))
        yield conn


def _as(identity, body, *args, **kwargs):
    """Call ``body(conn, *args, **kwargs)`` with the worker identity bound."""

    def _inner(conn: psycopg.Connection) -> Any:
        with transaction_identity(conn, _principal(identity)):
            return body(conn, *args, **kwargs)

    return _inner


def run_commit(pool, identity, attempt, target):
    with pool.connection() as conn:
        with transaction_identity(conn, _principal(identity)):
            return jobs_domain.commit_status(conn, attempt, target=target)


@pytest.fixture
def worker_pool(c13_dsn: str) -> Any:
    pool = _worker_pool(c13_dsn)
    yield pool
    pool.close()


@pytest.fixture
def job(c13_dsn, run_as, cast, library, worker_pool) -> UUID:
    record, _ = run_as(
        cast.contributor,
        lambda c: jobs_domain.submit_job(
            c, library_id=library, kind=JobKind.INGEST, idempotency_key="fence-0001"
        ),
    )
    return record.id


# ------------------------------------------------------------- only one winner


def test_two_connections_racing_for_one_lease_produce_exactly_one_winner(
    c13_dsn, admin, worker_identity, job, worker_pool
) -> None:
    """A real overlap: the second UPDATE waits on the first one's row lock."""
    holder = _conn(c13_dsn)
    try:
        with _holding(holder, worker_identity) as conn:
            # Written but not committed: the row lock is held and nobody else
            # can see the new status.
            first = jobs_domain.claim_attempt(conn, job)

            racer = Racer(c13_dsn, _as(worker_identity, jobs_domain.claim_attempt, job))
            pid = racer.start()
            _await_block(admin, pid)  # the overlap, observed rather than assumed
        # Leaving the block commits, and the racer is released here.

        racer.expect(LeaseUnavailable)
        assert first.job.status is ProcessingStatus.RUNNING
    finally:
        holder.close()


def test_a_takeover_computed_against_a_fence_that_moved_is_refused(
    c13_dsn, admin, worker_identity, job, worker_pool, run_as
) -> None:
    """Reading a fence and then using it has to be one decision.

    A live worker is heartbeating — renewing its lease, uncommitted, holding
    the row lock. A supervisor read the fence a moment earlier and is now
    trying to take the job over on the strength of it. It has to wait, and
    when it wakes up the token it holds is stale, so it must be refused: two
    things must never both believe they recovered the same dead attempt.
    """

    def act(conn: psycopg.Connection):
        with transaction_identity(conn, _principal(worker_identity)):
            live = jobs_domain.claim_attempt(conn, job)
            return live

    live = run_as(worker_identity.principal_id, act, pool=worker_pool)
    stale = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.current_fence(c, job), pool=worker_pool
    )
    assert stale == live.fence

    holder = _conn(c13_dsn)
    try:
        with _holding(holder, worker_identity) as conn:
            # A real renewal, left uncommitted: the row lock is held and the
            # committed row still carries the token the racer is holding.
            jobs_domain.renew_attempt(conn, live)

            racer = Racer(
                c13_dsn,
                _as(worker_identity, jobs_domain.take_over_attempt, job, stale_fence=stale),
            )
            pid = racer.start()
            _await_block(admin, pid)

        racer.expect(LeaseUnavailable)
    finally:
        holder.close()

    # The worker that was actually alive kept the job, and can still finish it.
    fresh = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.current_fence(c, job), pool=worker_pool
    )
    assert fresh != stale
    renewed = run_as(
        worker_identity.principal_id,
        lambda c: jobs_domain.take_over_attempt(c, job, stale_fence=fresh),
        pool=worker_pool,
    )
    assert renewed.fence != fresh


def test_a_stale_attempt_cannot_write_a_result_after_a_newer_one_won(
    run_as, worker_identity, job, world, worker_pool
) -> None:
    """The card's central claim, stated as a test.

    Attempt A claims the job and then stalls — a long parse, a partition, a
    worker the supervisor declared dead. Attempt B takes the lease over. A
    wakes up and tries to publish its result. It is refused, and the job still
    says whatever B left it saying.
    """
    stale = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.claim_attempt(c, job), pool=worker_pool
    )
    winner = run_as(
        worker_identity.principal_id,
        lambda c: jobs_domain.take_over_attempt(
            c, job, stale_fence=jobs_domain.current_fence(c, job)
        ),
        pool=worker_pool,
    )
    assert winner.fence != stale.fence

    with pytest.raises(LeaseLost):
        run_as(
            worker_identity.principal_id,
            lambda c: jobs_domain.commit_status(c, stale, target=ProcessingStatus.DONE),
            pool=worker_pool,
        )

    # Not refused politely and written anyway: the row is untouched.
    assert world.job_status(job) == "running"

    done = run_commit(worker_pool, worker_identity, winner, ProcessingStatus.DONE)
    assert done.status is ProcessingStatus.DONE


def test_the_database_rejects_the_stale_token_not_just_the_python(
    c13_dsn, run_as, worker_identity, job, worker_pool
) -> None:
    """The fence is a predicate inside the UPDATE, so a direct client cannot
    get round it either.

    If the refusal in the test above were Python raising before it ever asked
    the server, this hand-written statement would succeed. It is the WHERE
    clause doing the work, and this is the statement that proves it.
    """
    stale = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.claim_attempt(c, job), pool=worker_pool
    )
    run_as(
        worker_identity.principal_id,
        lambda c: jobs_domain.renew_attempt(c, stale),
        pool=worker_pool,
    )

    conn = _conn(c13_dsn)
    try:
        with transaction_identity(conn, _principal(worker_identity)):
            rows = conn.execute(
                "UPDATE kb.job SET status = 'done' WHERE id = %s AND xmin::text = %s",
                (job, stale.fence),
            ).rowcount
    finally:
        # The transaction context rolls back on the way out; the statement
        # matched nothing, so there was nothing to keep either way.
        conn.close()
    assert rows == 0


def test_a_stale_attempt_cannot_renew_either(run_as, worker_identity, job, worker_pool) -> None:
    """Renewal is a write on the lease, so a dead attempt must not be able to
    keep the lease alive by going on pretending to work."""
    stale = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.claim_attempt(c, job), pool=worker_pool
    )
    run_as(
        worker_identity.principal_id,
        lambda c: jobs_domain.take_over_attempt(
            c, job, stale_fence=jobs_domain.current_fence(c, job)
        ),
        pool=worker_pool,
    )
    with pytest.raises(LeaseLost):
        run_as(
            worker_identity.principal_id,
            lambda c: jobs_domain.renew_attempt(c, stale),
            pool=worker_pool,
        )


def test_renewal_hands_back_a_new_token(run_as, worker_identity, job, worker_pool) -> None:
    """The mirror image, and the easier bug to write.

    A renewal that returned the *same* token would fence the worker out of its
    own job one stage later, and the symptom would be a job that mysteriously
    fails after a while.
    """
    attempt = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.claim_attempt(c, job), pool=worker_pool
    )
    renewed = run_as(
        worker_identity.principal_id,
        lambda c: jobs_domain.renew_attempt(c, attempt),
        pool=worker_pool,
    )
    assert renewed.fence != attempt.fence
    done = run_commit(worker_pool, worker_identity, renewed, ProcessingStatus.DONE)
    assert done.status is ProcessingStatus.DONE


def test_a_finished_job_cannot_be_taken_over(
    run_as, worker_identity, job, world, worker_pool
) -> None:
    attempt = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.claim_attempt(c, job), pool=worker_pool
    )
    run_commit(worker_pool, worker_identity, attempt, ProcessingStatus.DONE)
    with pytest.raises(LeaseUnavailable):
        run_as(
            worker_identity.principal_id,
            lambda c: jobs_domain.take_over_attempt(
                c, job, stale_fence=jobs_domain.current_fence(c, job)
            ),
            pool=worker_pool,
        )
    assert world.job_status(job) == "done"


def test_a_needs_ocr_job_cannot_be_taken_over_either(
    run_as, worker_identity, job, world, worker_pool
) -> None:
    """``needs_ocr`` is an answer, not a failure waiting for a retry."""
    attempt = run_as(
        worker_identity.principal_id, lambda c: jobs_domain.claim_attempt(c, job), pool=worker_pool
    )
    run_commit(worker_pool, worker_identity, attempt, ProcessingStatus.NEEDS_OCR)
    with pytest.raises(LeaseUnavailable):
        run_as(
            worker_identity.principal_id,
            lambda c: jobs_domain.take_over_attempt(
                c, job, stale_fence=jobs_domain.current_fence(c, job)
            ),
            pool=worker_pool,
        )
    assert world.job_status(job) == "needs_ocr"
