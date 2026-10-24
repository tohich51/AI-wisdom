"""C13 — the canonical processing job: closed vocabulary, state machine, fencing.

Everything the worker needs to know about a job is read from the ``kb.job``
row. The queue message contributes exactly one value — the job id — and every
other fact, including *who* is acting, is resolved from the database. A caller
that puts an ``actor_id`` (or a library, or a status, or a fence) into the
payload is not believed; see ``kb.processing.worker``.

Three properties are load-bearing and each one is enforced by PostgreSQL
rather than by this module being careful:

**Closed status.** ``kb.job.status`` is the ``processing_status`` enum
(``queued``, ``running``, ``partial``, ``needs_ocr``, ``failed``, ``done``).
:func:`parse_status` turns a database value into :class:`ProcessingStatus` and
raises :class:`UnknownJobStatus` for anything else. A status this card does not
know about is a bug to be reported, not a string to be stored.

**Idempotency.** ``UNIQUE (library_id, idempotency_key)`` already exists.
:func:`submit_job` uses ``ON CONFLICT DO NOTHING`` and then reads the existing
row. It never issues an ``UPDATE``, which matters for more than tidiness: an
``UPDATE`` would bump the row's xmin, and xmin is the fence (below). A retried
submission therefore cannot create a second job *and* cannot disturb an attempt
that is already running.

**Attempt fencing.** The fence token is the row's ``xmin`` — the xid of the
transaction that last wrote it. Claiming a job is an ``UPDATE`` that returns
its own xid; every later write carries ``AND xmin::text = <token>``. Any other
transaction that touches the row changes the token, so a worker whose lease was
taken over writes zero rows and is told so.

``xmin`` is a real, database-enforced fence, not a convention: the predicate is
evaluated inside the ``UPDATE`` while the row lock is held, so two racing
connections are serialised by PostgreSQL itself.

It is also not free, and pretending otherwise would be the failure mode here.
What the choice costs is written out in :data:`FENCE_IMPLEMENTATION` below and
repeated in ``docs/handoff/results/C13.json``; a dedicated ``kb.job.attempt``
column would be plainer and is proposed to the single DDL owner, but 0001 is
not this card's file to edit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NewType
from uuid import UUID

from kb.contracts.enums import ProcessingStatus

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CLAIMABLE_FROM",
    "FENCE_IMPLEMENTATION",
    "TERMINAL_STATUSES",
    "Attempt",
    "Fence",
    "JobError",
    "JobKind",
    "JobNotFound",
    "JobRecord",
    "LeaseLost",
    "LeaseUnavailable",
    "ProcessingStatus",
    "UnknownJobKind",
    "UnknownJobStatus",
    "claim_attempt",
    "commit_status",
    "current_fence",
    "is_terminal",
    "may_transition",
    "parse_kind",
    "parse_status",
    "read_job",
    "renew_attempt",
    "requeue_attempt",
    "submit_job",
    "take_over_attempt",
]


FENCE_IMPLEMENTATION = """
kb.job has no attempt column. 0001 is the single DDL owner and is outside this
card's allowed paths, so the fence is the row's xmin:

  * claim    UPDATE ... RETURNING xmin   -> the leaser holds its own xid
  * renew    UPDATE ... WHERE xmin = t   -> a new xid, so renew returns a new
                                            token rather than keeping the old
  * takeover UPDATE ... WHERE xmin = t   -> any other transaction invalidates t
  * commit   UPDATE ... WHERE xmin = t   -> 0 rows means "you lost the lease"

This is enforced by the server, not by this module. What it costs:

  * xid wraparound. A token is a 32-bit transaction id, so after ~4.29e9
    transactions a token could in principle repeat. A stale attempt would have
    to survive that many writes while holding a string, which no real scheduler
    does, but it is a bound and not an impossibility.
  * no separate attempt *number*. Operators cannot read "attempt 3 of 5" off
    the row; they read the queue. See the findings in the C13 result file.
  * a status change and a fence change are the same event, by construction.

Proposed DDL for the single owner, not applied here:
    ALTER TABLE kb.job
        ADD COLUMN attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
        ADD COLUMN lease_expires_at timestamptz,
        ADD COLUMN actor_id uuid;
"""


class JobKind(StrEnum):
    """The job vocabulary, mirroring the ``kb.job.kind`` CHECK constraint.

    This duplicates a contract vocabulary rather than living in
    ``kb/contracts/enums.py``, because that file belongs to another card's
    owner. It is deliberately closed for the same reason: an unknown kind is
    an error, not a free string.
    """

    INGEST = "ingest"
    EXTRACT = "extract"
    EMBED = "embed"
    COMPILE = "compile"
    EXPORT = "export"


class JobError(Exception):
    """Base for every refusal this module makes."""


class UnknownJobStatus(JobError):
    """A status outside the closed ``processing_status`` vocabulary."""


class UnknownJobKind(JobError):
    """A kind outside the closed ``kb.job.kind`` vocabulary."""


class JobNotFound(JobError):
    """No such job is visible to this identity.

    The same answer whether the job does not exist, belongs to a library the
    caller cannot see, or was deleted with its library. Telling those apart
    would turn job ids into a directory of invisible work.
    """


class LeaseUnavailable(JobError):
    """Another attempt holds the job. Not an error: a normal race outcome."""


class LeaseLost(JobError):
    """This attempt is no longer the current one and must not write."""


Fence = NewType("Fence", str)
"""The row's xmin, as text. Opaque to callers; never constructed by hand."""


def parse_status(raw: object) -> ProcessingStatus:
    """Turn a database value into the closed status enum.

    ``str(ProcessingStatus.X)`` is the value's own text under ``StrEnum``, so
    the round trip is exact. Anything else raises — including a value that
    happens to be a valid-looking string.
    """
    if isinstance(raw, ProcessingStatus):
        return raw
    try:
        return ProcessingStatus(str(raw))
    except ValueError as exc:
        raise UnknownJobStatus(
            f"{raw!r} is not a member of the closed processing_status vocabulary"
        ) from exc


def parse_kind(raw: object) -> JobKind:
    """Turn a database value into the closed job-kind enum."""
    if isinstance(raw, JobKind):
        return raw
    try:
        return JobKind(str(raw))
    except ValueError as exc:
        raise UnknownJobKind(f"{raw!r} is not a member of the closed job kind vocabulary") from exc


# ---------------------------------------------------------------- the machine

#: Statuses a worker never leaves on its own. ``needs_ocr`` is here on
#: purpose: an unsupported scan is a real outcome, not a failure to retry
#: forever. No OCR is promised in v1, so retrying cannot fix it, and a job that
#: spins on it would burn a worker slot and hide the reason from the operator.
TERMINAL_STATUSES: frozenset[ProcessingStatus] = frozenset(
    {ProcessingStatus.DONE, ProcessingStatus.NEEDS_OCR}
)

#: The whole state machine, as data. A transition that is not named here is
#: refused by :func:`may_transition`, and the refusal is a test, not a
#: convention.
ALLOWED_TRANSITIONS: dict[ProcessingStatus, frozenset[ProcessingStatus]] = {
    ProcessingStatus.QUEUED: frozenset({ProcessingStatus.RUNNING, ProcessingStatus.FAILED}),
    ProcessingStatus.RUNNING: frozenset(
        {
            ProcessingStatus.QUEUED,  # the attempt failed; it will be retried
            ProcessingStatus.PARTIAL,
            ProcessingStatus.NEEDS_OCR,
            ProcessingStatus.FAILED,
            ProcessingStatus.DONE,
        }
    ),
    # A partial job is resumed, never silently overwritten: the stages that
    # already committed stay committed and the ladder starts where it stopped.
    ProcessingStatus.PARTIAL: frozenset(
        {
            ProcessingStatus.QUEUED,
            ProcessingStatus.RUNNING,
            ProcessingStatus.PARTIAL,
            ProcessingStatus.NEEDS_OCR,
            ProcessingStatus.FAILED,
            ProcessingStatus.DONE,
        }
    ),
    ProcessingStatus.FAILED: frozenset({ProcessingStatus.QUEUED}),
    ProcessingStatus.DONE: frozenset(),
    ProcessingStatus.NEEDS_OCR: frozenset(),
}


def may_transition(current: ProcessingStatus, target: ProcessingStatus) -> bool:
    """True when ``current -> target`` is a declared edge of the machine."""
    return target in ALLOWED_TRANSITIONS[current]


def is_terminal(status: ProcessingStatus) -> bool:
    """True when no worker transition leaves ``status`` on its own."""
    return status in TERMINAL_STATUSES


# --------------------------------------------------------------- the record


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One row of ``kb.job``, already validated against the closed vocabularies.

    ``fence`` is the token a *new* attempt would have to beat. It is not a
    lease: reading a record never claims anything.
    """

    id: UUID
    library_id: UUID
    kind: JobKind
    status: ProcessingStatus
    idempotency_key: str
    index_target: str | None
    index_generation: int
    created_at: dt.datetime
    fence: Fence

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> JobRecord:
        (
            job_id,
            library_id,
            kind,
            status,
            idempotency_key,
            index_target,
            index_generation,
            created_at,
            fence,
        ) = row
        return cls(
            id=job_id,
            library_id=library_id,
            kind=parse_kind(kind),
            status=parse_status(status),
            idempotency_key=idempotency_key,
            index_target=index_target,
            index_generation=index_generation,
            created_at=created_at,
            fence=Fence(str(fence)),
        )


@dataclass(frozen=True, slots=True)
class Attempt:
    """A held lease.

    An ``Attempt`` is a capability, not a snapshot: it is only usable while the
    row's xmin still equals ``fence``. Every write in this module takes one and
    checks it, so a stale attempt cannot write even if it holds one.
    """

    job: JobRecord
    fence: Fence

    @property
    def id(self) -> UUID:
        return self.job.id

    @property
    def library_id(self) -> UUID:
        return self.job.library_id


# Every statement below is a single string literal, and the only things that
# reach the server are the %s parameters. S608 exists to stop request data
# being pasted into SQL; none of these fragments is anything but a literal, and
# the column list is repeated rather than composed with "+" so that the rule
# stays switched on for this file rather than being silenced for it.
_SELECT_BY_ID = (
    "SELECT id, library_id, kind, status, idempotency_key, index_target, "
    "index_generation, created_at, xmin::text FROM kb.job WHERE id = %s"
)
_SELECT_BY_KEY = (
    "SELECT id, library_id, kind, status, idempotency_key, index_target, "
    "index_generation, created_at, xmin::text FROM kb.job "
    "WHERE library_id = %s AND idempotency_key = %s"
)
_SELECT_FENCE = "SELECT xmin::text FROM kb.job WHERE id = %s"
_INSERT_JOB = (
    "INSERT INTO kb.job (library_id, kind, idempotency_key, index_target, index_generation) "
    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (library_id, idempotency_key) DO NOTHING "
    "RETURNING id, library_id, kind, status, idempotency_key, index_target, "
    "index_generation, created_at, xmin::text"
)
_CLAIM = (
    "UPDATE kb.job SET status = 'running' WHERE id = %s AND status = ANY(%s) "
    "RETURNING id, library_id, kind, status, idempotency_key, index_target, "
    "index_generation, created_at, xmin::text"
)
_TAKEOVER = (
    "UPDATE kb.job SET status = 'running' "
    "WHERE id = %s AND xmin::text = %s AND status = 'running' "
    "RETURNING id, library_id, kind, status, idempotency_key, index_target, "
    "index_generation, created_at, xmin::text"
)
# A no-op on the data that is a real write on the lease: it moves xmin, which is
# what makes the old token stop matching. Renewal therefore hands back a new
# Attempt rather than the one it was given.
_RENEW = (
    "UPDATE kb.job SET status = status WHERE id = %s AND xmin::text = %s "
    "RETURNING id, library_id, kind, status, idempotency_key, index_target, "
    "index_generation, created_at, xmin::text"
)
_COMMIT_STATUS = (
    "UPDATE kb.job SET status = %s WHERE id = %s AND xmin::text = %s AND status = %s "
    "RETURNING id, library_id, kind, status, idempotency_key, index_target, "
    "index_generation, created_at, xmin::text"
)


# ------------------------------------------------------------------- reads


def read_job(conn: Any, job_id: UUID) -> JobRecord:
    """Read one job. Takes no lock and cannot wait on the processing queue.

    A plain ``SELECT`` under MVCC reads the last *committed* version even while
    another connection holds an uncommitted write on the same row, so the UI
    asking "what stage is this at?" is never parked behind a stage that is
    taking a long time. The statement timeout is a belt-and-braces bound: it is
    the reason a stalled writer cannot turn into a stalled reader, not a
    substitute for not locking.
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute(_SELECT_BY_ID, (job_id,))
        row = cur.fetchone()
    if row is None:
        raise JobNotFound("no such job")
    return JobRecord.from_row(row)


def current_fence(conn: Any, job_id: UUID) -> Fence:
    """The token a takeover must beat. Reading a fence claims nothing."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_FENCE, (job_id,))
        row = cur.fetchone()
    if row is None:
        raise JobNotFound("no such job")
    return Fence(str(row[0]))


# --------------------------------------------------------------- submission


def submit_job(
    conn: Any,
    *,
    library_id: UUID,
    kind: JobKind,
    idempotency_key: str,
    index_target: str | None = None,
    index_generation: int = 1,
) -> tuple[JobRecord, bool]:
    """Create the job, or return the one that already exists.

    Returns ``(job, created)``. The second element answers "did this
    submission start anything", which is the only thing a caller may branch on.

    The statement is ``INSERT ... ON CONFLICT DO NOTHING``: on a retry the
    existing row is read back and *nothing is written*. That is deliberate
    rather than incidental — an ``ON CONFLICT DO UPDATE`` would bump the row's
    xmin and silently invalidate the lease of an attempt that is running right
    now, so a retried submission could restart work already in progress.

    Runs as the submitting identity (``kb_app``): ``kb_worker`` is granted
    ``SELECT, UPDATE`` on ``kb.job`` and deliberately not ``INSERT``, so a
    worker cannot invent work.
    """
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_JOB,
            (library_id, str(kind), idempotency_key, index_target, index_generation),
        )
        row = cur.fetchone()
        created = row is not None
        if row is None:
            cur.execute(_SELECT_BY_KEY, (library_id, idempotency_key))
            row = cur.fetchone()
            if row is None:
                # The conflicting row is not visible to this identity. Saying
                # so would confirm the key exists in a library the caller
                # cannot see.
                raise JobNotFound("no such job")
    return JobRecord.from_row(row), created


# ----------------------------------------------------------------- the lease

#: Statuses a fresh attempt may be claimed from. ``done`` and ``needs_ocr`` are
#: absent on purpose: both are terminal, so no worker may take them over.
#: ``running`` is absent because a job that is already running has an attempt,
#: and the way to replace a dead one is :func:`take_over_attempt`, which says
#: out loud that it is superseding someone.
CLAIMABLE_FROM: frozenset[ProcessingStatus] = frozenset(
    {ProcessingStatus.QUEUED, ProcessingStatus.PARTIAL, ProcessingStatus.FAILED}
)


def claim_attempt(conn: Any, job_id: UUID) -> Attempt:
    """Take the lease on a job nobody is working on.

    The ``WHERE`` clause is the whole mechanism: two connections calling this
    at the same moment serialise on the row lock, the loser re-evaluates the
    clause against the committed row, finds ``status = 'running'``, and gets
    zero rows back. It is then told :class:`LeaseUnavailable`, which is a
    normal outcome and not an error.
    """
    with conn.cursor() as cur:
        cur.execute(_CLAIM, (job_id, list(CLAIMABLE_FROM)))
        row = cur.fetchone()
    if row is None:
        raise LeaseUnavailable("another attempt holds this job, or it is finished")
    record = JobRecord.from_row(row)
    return Attempt(job=record, fence=record.fence)


def take_over_attempt(conn: Any, job_id: UUID, *, stale_fence: Fence) -> Attempt:
    """Supersede a dead attempt.

    This is what a restarted worker, or a supervisor that declared a worker
    dead, calls. The caller must say which token it believes is current, read
    from :func:`current_fence`; if anything changed in between the update
    matches nothing and :class:`LeaseUnavailable` is raised, so two
    supervisors cannot both "recover" the same job and both believe they won.

    The old holder is not asked. Its token stops matching the moment this
    commits, and its next write returns zero rows.
    """
    with conn.cursor() as cur:
        cur.execute(_TAKEOVER, (job_id, stale_fence))
        row = cur.fetchone()
    if row is None:
        raise LeaseUnavailable("the attempt moved on; nothing to take over")
    record = JobRecord.from_row(row)
    return Attempt(job=record, fence=record.fence)


def renew_attempt(conn: Any, attempt: Attempt) -> Attempt:
    """Extend a lease the worker is still alive and using.

    Renewal *is* a write, so it changes xmin and the caller gets a new
    :class:`Attempt`. Handing back the old token would be the classic fencing
    bug: a worker that renewed and then wrote with its previous token would
    fence itself out of its own job.
    """
    with conn.cursor() as cur:
        cur.execute(_RENEW, (attempt.id, attempt.fence))
        row = cur.fetchone()
    if row is None:
        raise LeaseLost("this attempt is no longer current")
    record = JobRecord.from_row(row)
    return Attempt(job=record, fence=record.fence)


def _fenced_update(
    conn: Any,
    attempt: Attempt,
    *,
    sql: str,
    params: tuple[Any, ...],
) -> JobRecord:
    """Run one guarded write and return the committed row.

    Every write a worker makes to a job goes through here. The fence is the
    first predicate, so a superseded attempt updates nothing, and the status
    is the second, so an attempt cannot move a job somewhere the state machine
    does not allow.

    When the guarded write matches nothing, the row is read back once to say
    *why*. A missing row is :class:`JobNotFound`; a different xmin is
    :class:`LeaseLost`; the same xmin with a different status is a genuinely
    illegal transition. Guessing between those three would turn a real
    operational fault into a silent no-op.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is not None:
            return JobRecord.from_row(row)
        cur.execute(_SELECT_BY_ID, (attempt.id,))
        current = cur.fetchone()
    if current is None:
        raise JobNotFound("no such job")
    record = JobRecord.from_row(current)
    if record.fence != attempt.fence:
        raise LeaseLost("this attempt is no longer current; its result was not written")
    if not may_transition(record.status, attempt.job.status):
        raise LeaseLost(f"illegal transition {record.status.value} -> {attempt.job.status.value}")
    raise LeaseLost("the guarded write matched nothing")


def commit_status(
    conn: Any,
    attempt: Attempt,
    *,
    target: ProcessingStatus,
) -> JobRecord:
    """Move a held job to ``target``, refusing the whole write if the lease is gone.

    This is *the* fence. Everything a worker publishes about a job's outcome
    goes through it, so "a worker that lost its lease cannot write a result"
    is a statement about the database, not about the worker's discipline.
    """
    if not may_transition(attempt.job.status, target):
        raise JobError(
            f"the state machine has no edge {attempt.job.status.value} -> {target.value}"
        )
    return _fenced_update(
        conn,
        attempt,
        sql=_COMMIT_STATUS,
        params=(target.value, attempt.id, attempt.fence, attempt.job.status.value),
    )


def requeue_attempt(conn: Any, attempt: Attempt) -> JobRecord:
    """Hand a failed attempt back to the queue without losing the job.

    The status goes back to ``queued``; the job row is not deleted and its
    idempotency key is not changed, so the retry is the same job and a further
    duplicate submission still resolves to it.
    """
    return commit_status(conn, attempt, target=ProcessingStatus.QUEUED)
