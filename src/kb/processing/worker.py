"""C13 — the worker entrypoint.

The rule this module exists to enforce: **the queue payload is not a source of
identity.** Every task function takes exactly one argument, ``job_id``, and
every other fact is read from ``kb.job`` on a connection whose identity is the
worker's own service account. A message that carries ``actor_id``, or a
library, or a status, or a fence, is refused by the closed task signature
before any of it is looked at — and if a future change somehow widened that
signature, the RLS policies would still decide what the write was allowed to
touch.

The work is one stage per task. Each stage task:

1. opens a transaction, binds the worker identity, and takes the lease —
   claiming the job, or taking it over from a worker that is gone;
2. refuses to run at all if the job has already reached a terminal status;
3. checks the quota gate before doing anything expensive;
4. runs the stage, whose writes are idempotent by content;
5. commits the resulting status **in the same transaction** as the stage's own
   writes, through the fence.

A kill between 1 and 5 is the interesting case and the two transactions are
what make it survivable: the lease is already committed, the work is not, so a
restart finds a ``running`` job whose stage produced nothing and repeats that
stage. It does not double it, because the stage's domain rows are keyed by
content and the fenced commit cannot succeed twice.

What each stage actually does is bounded by what the catalogue schema can
record, and that is stated per stage below rather than glossed:

* ``parse`` advances ``kb.source.processing`` for every source still
  ``queued``. Real table, real work, idempotent because a source that is no
  longer ``queued`` is not touched.
* ``index`` checks the library's opt-in and reports it. It cannot open a
  generation: ``kb.index_generation`` has no write policy for any runtime
  role, which is a schema gap C13 reports rather than works around.
* ``embed``, ``extract_cloud`` and ``maintain`` have **no** table in
  0001-0005 that could record a result. They run their gates and commit their
  status through the fence, and the durable per-stage record is the queue row.

Together those are the schema's limits, not this card's design. They are listed
with the DDL that would close them in ``docs/handoff/results/C13.json``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from kb.access.policy import Principal, transaction_identity
from kb.contracts.enums import ProcessingStatus
from kb.processing import jobs as jobs_domain
from kb.processing import jobs_queue
from kb.processing.jobs import Attempt, JobRecord
from kb.processing.stages import Stage, StageOutcome, first_pending, ladder_for

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg
    from procrastinate import App
    from procrastinate.job_context import JobContext

__all__ = [
    "JobWorkerKilled",
    "QuotaPaused",
    "SourceNeedsOcr",
    "StageFailure",
    "WorkerIdentity",
    "build_worker",
    "run_stage",
    "submit_and_enqueue",
]

logger = logging.getLogger(__name__)


class StageFailure(RuntimeError):
    """A stage failed for a reason that may pass. The only retried exception."""


class QuotaPaused(RuntimeError):
    """Generative stages are not enabled for this library.

    Not a :class:`StageFailure`, and never raised out of the task: the stage is
    re-queued and the task returns, so the worker's concurrency slot is
    released immediately. A paused quota is a policy answer, and a worker
    holding a slot while it waits for an owner's subscription is the failure
    the card names.
    """


class SourceNeedsOcr(RuntimeError):
    """An unsupported scan. Terminal for the job, never retried."""

    def __init__(self, source_id: UUID, media_type: str) -> None:
        super().__init__(f"source {source_id} ({media_type}) needs OCR, which v1 does not do")
        self.source_id = source_id
        self.media_type = media_type


class JobWorkerKilled(RuntimeError):
    """The worker process died mid-stage. Never a normal return path."""


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """Who the worker is, as configured by the deployment.

    Process configuration, not a message field. A deployment that forgot to
    set it has no identity at all, the RLS policies see nothing, and the job
    simply does not run — the correct direction to fail in.
    """

    principal_id: UUID
    account_id: UUID

    def principal(self) -> Principal:
        return Principal(
            principal_id=self.principal_id,
            account_id=self.account_id,
            generation_watermark=1,
        )


# --------------------------------------------------------------- stage bodies

#: Media types that are page images. PRODUCT-SPEC: an unsupported scan becomes
#: ``needs_ocr`` and full OCR is not promised in v1, so the honest answer is to
#: say so — not to return an empty document that reads like a successful parse.
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/tiff", "image/gif"})


def parse_stage(conn: psycopg.Connection, *, job: JobRecord) -> StageOutcome:
    """Advance every source of the library that is still ``queued``.

    A page image is an unsupported scan: the source is moved to ``needs_ocr``
    and the stage stops there, because a book cannot be half parsed. Anything
    else moves to ``running``, which is the per-source checkpoint a later stage
    and a later restart both read.

    Idempotent by construction — sources that are no longer ``queued`` are not
    touched — so running this stage twice after a kill writes nothing new.
    """
    rows = conn.execute(
        "SELECT id, media_type FROM kb.source "
        "WHERE library_id = %s AND processing = 'queued' ORDER BY created_at, id",
        (job.library_id,),
    ).fetchall()
    for source_id, media_type in rows:
        if media_type in IMAGE_MEDIA_TYPES:
            conn.execute(
                "UPDATE kb.source SET processing = 'needs_ocr' "
                "WHERE id = %s AND processing = 'queued'",
                (source_id,),
            )
            raise SourceNeedsOcr(source_id, media_type)
        conn.execute(
            "UPDATE kb.source SET processing = 'running' WHERE id = %s AND processing = 'queued'",
            (source_id,),
        )
    return StageOutcome.COMMITTED


def embed_stage(conn: psycopg.Connection, *, job: JobRecord) -> StageOutcome:
    """Select the sources that are ready to be embedded.

    0001-0005 carry no table that records an embedding, so this stage has
    nowhere durable to write a vector. It performs the selection that decides
    the work and logs the count; the durable per-stage record is the queue row.
    Reported as a finding rather than hidden.
    """
    row = conn.execute(
        "SELECT count(*) FROM kb.source WHERE library_id = %s AND processing = 'running'",
        (job.library_id,),
    ).fetchone()
    count = row[0] if row is not None else 0
    logger.info("embed stage selected %s sources for library %s", count, job.library_id)
    return StageOutcome.COMMITTED


def extract_cloud_stage(conn: psycopg.Connection, *, job: JobRecord) -> StageOutcome:
    """Refuse to spend a subscription that is not switched on for this library.

    The check is first and nothing is written before it, so a paused quota
    costs one indexed lookup and no CPU. Global concurrency 1 across the
    installation is C35's scaling work and is not simulated here.
    """
    row = conn.execute(
        "SELECT generation_allowed FROM kb.generation_policy WHERE library_id = %s",
        (job.library_id,),
    ).fetchone()
    allowed = bool(row[0]) if row is not None else False
    message = jobs_queue.quota_paused_message(Stage.EXTRACT_CLOUD, generation_allowed=allowed)
    if message is not None:
        raise QuotaPaused(message)
    return StageOutcome.COMMITTED


def index_stage(conn: psycopg.Connection, *, job: JobRecord) -> StageOutcome:
    """Decide whether this job's index generation may be opened at all.

    It may not, today, and that is the schema talking rather than a choice
    here. ``kb.index_generation`` has one policy — ``index_generation_read``,
    a SELECT — and no write policy for *any* runtime role, so no worker,
    gateway or otherwise can open a generation. The same shape C10 found on
    ``kb.source_version``: 0002's blanket GRANT is unreachable because RLS has
    no policy to admit it.

    So the stage performs the check it can perform, refuses a generation that
    the library has not opted into, and leaves the durable record to the queue
    row. C13 proposes the missing ``index_generation_write`` policy for the
    single DDL owner; see ``docs/handoff/results/C13.json``. Until that lands,
    this stage is a checkpoint and nothing pretends otherwise.
    """
    row = conn.execute(
        "SELECT generation_allowed FROM kb.generation_policy WHERE library_id = %s",
        (job.library_id,),
    ).fetchone()
    if row is None or not bool(row[0]):
        logger.info(
            "index stage for job %s: library %s has not opted into an index generation",
            job.id,
            job.library_id,
        )
    return StageOutcome.COMMITTED


def maintain_stage(conn: psycopg.Connection, *, job: JobRecord) -> StageOutcome:
    """Housekeeping. No table in 0001-0005 records it; the queue row does."""
    return StageOutcome.COMMITTED


#: One body per stage. A stage with no entry, or an entry with no stage, is a
#: bug in the ladder; the suite checks the two sets are the same.
STAGE_BODIES: dict[Stage, Callable[..., StageOutcome]] = {
    Stage.PARSE: parse_stage,
    Stage.EMBED: embed_stage,
    Stage.EXTRACT_CLOUD: extract_cloud_stage,
    Stage.INDEX: index_stage,
    Stage.MAINTAIN: maintain_stage,
}


# ------------------------------------------------------------------ the run


def _acquire(conn: psycopg.Connection, job_id: UUID) -> Attempt | None:
    """Take the lease, or return ``None`` if the job is already finished.

    A job in ``running`` is taken over rather than refused. Procrastinate does
    not hand one task to two workers at a time, so a ``running`` job with no
    task in flight belongs to a worker that died; the token it held is exactly
    what is being superseded, and its next write will match nothing.
    """
    job = jobs_domain.read_job(conn, job_id)
    if jobs_domain.is_terminal(job.status):
        return None
    if job.status is ProcessingStatus.RUNNING:
        return jobs_domain.take_over_attempt(
            conn, job_id, stale_fence=jobs_domain.current_fence(conn, job_id)
        )
    return jobs_domain.claim_attempt(conn, job_id)


def _finish(
    conn: psycopg.Connection,
    attempt: Attempt,
    stage: Stage,
    outcome: StageOutcome,
) -> StageOutcome:
    """Decide where the job goes, and write it under the fence.

    ``completed`` is the queue's own record of which stages are done, plus the
    stage that has just run — its own row only becomes ``succeeded`` when this
    task returns, which is after this transaction commits.
    """
    if not outcome.is_success:
        target = (
            ProcessingStatus.FAILED
            if outcome is StageOutcome.FAILED_FINAL
            else ProcessingStatus.QUEUED
        )
        jobs_domain.commit_status(conn, attempt, target=target)
        return outcome

    done = jobs_queue.completed_stages(conn, attempt.id) | {stage}
    if first_pending(ladder_for(attempt.job.kind), done) is None:
        jobs_domain.commit_status(conn, attempt, target=ProcessingStatus.DONE)
    return outcome


def run_stage(
    conn: psycopg.Connection,
    identity: WorkerIdentity,
    *,
    job_id: UUID,
    stage: Stage,
    on_kill: Callable[[Attempt], None] | None = None,
) -> StageOutcome:
    """Claim the lease, run one stage, commit the outcome under the fence.

    Two transactions, on purpose. The lease commits first, so a worker that
    dies in the second one leaves a ``running`` job — which is how a restart
    knows to take over. The stage's writes and the job's status commit
    together, so a job's status can never claim a stage whose rows were rolled
    back.

    ``on_kill`` is called inside the second transaction, between the lease and
    the work. It exists so the suite can kill a worker at exactly the point
    that makes a restart interesting. Production never passes it.
    """
    with transaction_identity(conn, identity.principal()):
        attempt = _acquire(conn, job_id)
    if attempt is None:
        with transaction_identity(conn, identity.principal()):
            return _outcome_for(jobs_domain.read_job(conn, job_id).status)

    with transaction_identity(conn, identity.principal()):
        if on_kill is not None:
            on_kill(attempt)
        try:
            outcome = STAGE_BODIES[stage](conn, job=attempt.job)
        except SourceNeedsOcr:
            # A real terminal answer, not a failure to retry. The stage's own
            # source rows are committed with it, so the reason survives.
            jobs_domain.commit_status(conn, attempt, target=ProcessingStatus.NEEDS_OCR)
            return StageOutcome.NEEDS_OCR
        except QuotaPaused:
            # Back to `queued`: the stage never ran, and the ladder must know
            # that. The task returns normally, so the slot is released now.
            jobs_domain.requeue_attempt(conn, attempt)
            return StageOutcome.PAUSED
        return _finish(conn, attempt, stage, outcome)


def _outcome_for(status: ProcessingStatus) -> StageOutcome:
    if status is ProcessingStatus.NEEDS_OCR:
        return StageOutcome.NEEDS_OCR
    return StageOutcome.COMMITTED


# ------------------------------------------------------------ the task layer


def submit_and_enqueue(
    app: App,
    conn: psycopg.Connection,
    *,
    job_id: UUID,
) -> list[int]:
    """Queue every stage this job still owes, inside ``conn``'s transaction."""
    completed = jobs_queue.completed_stages(conn, job_id)
    job = jobs_domain.read_job(conn, job_id)
    if jobs_domain.is_terminal(job.status):
        return []
    pending = tuple(stage for stage in ladder_for(job.kind) if stage not in completed)
    if not pending:
        return []
    return jobs_queue.enqueue_stages(app, conn, job_id=job_id, stages=pending)


def build_worker(
    app: App,
    identity: WorkerIdentity,
    *,
    connect: Callable[[], psycopg.Connection],
) -> App:
    """Register one task per stage on ``app``.

    Every task has the same closed signature and no other argument. That is the
    mechanism behind "the queue payload cannot set somebody else's actor": there
    is no parameter a message could use to say who the worker is, and the
    identity comes from ``identity``, which is deployment configuration.

    ``connect`` yields a connection. The task body opens a transaction through
    :func:`run_stage` and closes it, so a pooled connection can never carry an
    identity into the next task.
    """
    for stage in jobs_queue.QUEUE_FOR_STAGE:
        _register_stage_task(app, stage, identity, connect=connect)
    return app


def _register_stage_task(
    app: App,
    stage: Stage,
    identity: WorkerIdentity,
    *,
    connect: Callable[[], psycopg.Connection],
) -> None:
    from kb.processing import jobs_queue as queue_module

    def _execute(job_id: str) -> None:
        """The blocking half: a database, a book, a fence. All synchronous."""
        with connect() as conn:
            outcome = run_stage(conn, identity, job_id=UUID(job_id), stage=stage)
        if not outcome.is_success:
            raise StageFailure(f"stage {stage.value} of job {job_id} ended {outcome.value}")

    @app.task(
        name=queue_module.stage_task_name(stage),
        queue=queue_module.QUEUE_FOR_STAGE[stage],
        retry=queue_module.stage_retry(),
        pass_context=True,
    )
    async def run_one_stage(context: JobContext, *, job_id: str) -> None:
        """Run one stage of one job. The payload is the job id and nothing else.

        Async so the worker can run several stages at once without one slow
        book blocking every other queue: the blocking work goes to a worker
        thread and the event loop stays free to fetch the next task.
        """
        del context  # the queue job id is the worker's, not the catalogue's
        await asyncio.to_thread(_execute, job_id)
