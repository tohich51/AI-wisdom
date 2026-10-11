"""C13 — the real queue. Procrastinate on the same PostgreSQL as everything else.

This module is the only place that talks to Procrastinate, and it does so
through the library's own API rather than by writing SQL against its tables.
Two of its decisions are worth stating up front.

**The enqueue joins the caller's transaction.** Procrastinate 3.10 accepts an
external connection on ``Task.configure(connection=...)``, and its deferral
goes through the ``procrastinate_defer_jobs_v1`` function declared in its own
schema. So the canonical job row and its queue message are written by one
transaction: either both exist or neither does. That is the outcome
ARCHITECTURE §7.2 asks for, using the supported path rather than a
hand-rolled outbox table, which is also a second DDL path for objects this
project has decided it does not want.

**The queue is the per-stage checkpoint.** ``kb.job`` has one status column
and a five-stage ladder does not fit in one column, so each stage is its own
deferred task and "this stage finished" is "this task is ``succeeded``".
Procrastinate writes that status only after the task body has returned, and
the body only returns after the stage's guarded write has committed — so the
checkpoint cannot claim a stage that did not happen, and cannot lose one that
did. These are the library's own bookkeeping tables read by the library's own
worker; they are not exposed as the user-facing job API, which is
``kb.job`` through :mod:`kb.processing.jobs`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from kb.processing.stages import QUOTA_GATED_STAGES, Stage

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg
    from procrastinate import App

__all__ = [
    "MAX_ATTEMPTS",
    "QUEUE_FOR_STAGE",
    "STAGE_TASK_PREFIX",
    "TaskAlreadyEnqueued",
    "apply_queue_schema",
    "build_app",
    "build_worker_app",
    "completed_stages",
    "enqueue_stages",
    "quota_paused_message",
    "stage_queueing_lock",
    "stage_retry",
    "stage_task_name",
    "task_attempts",
]


#: One Procrastinate task per stage. The name is derived from the stage value,
#: so the queue can be read back without a second lookup table that could
#: disagree with the ladder.
STAGE_TASK_PREFIX = "kb.stage."

#: Each stage gets its own queue so a worker can be given only the cheap
#: stages. A deployment that wants the cloud stage held back configures the
#: worker, not the code.
QUEUE_FOR_STAGE: dict[Stage, str] = {
    Stage.PARSE: "kb-parse",
    Stage.EMBED: "kb-embed",
    Stage.EXTRACT_CLOUD: "kb-cloud",
    Stage.INDEX: "kb-index",
    Stage.MAINTAIN: "kb-maintain",
}

#: Bounded retries. A stage that keeps failing must stop, or one broken book
#: turns into a job that never ends and a queue that never drains. Four
#: attempts with an exponential backoff is the budget; what happens after that
#: is ``FAILED_FINAL`` and a job in ``failed`` waiting for a human.
MAX_ATTEMPTS = 4


class TaskAlreadyEnqueued(RuntimeError):
    """A stage of this job is already waiting in the queue.

    Raised rather than swallowed: a duplicate submission is normal, but a
    caller that quietly gets "ok, queued" for a stage that is already pending
    would believe work was scheduled when it was not.
    """


def build_app(conninfo: str) -> App:
    """The queue application for the *submitting* side, on the catalogue's server.

    A sync connector, because the enqueue runs on the gateway's own
    synchronous connection inside the gateway's own transaction. Running a
    worker needs the other kind; :func:`build_worker_app` is that one. Both
    point at the same database, and therefore at the same queue.
    """
    from procrastinate import App
    from procrastinate.sync_psycopg_connector import SyncPsycopgConnector

    return App(connector=SyncPsycopgConnector(conninfo=conninfo))


def build_worker_app(app: App, conninfo: str) -> App:
    """A second application for running a worker, sharing one task registry.

    Procrastinate splits these deliberately: deferring onto somebody else's
    transaction is synchronous, and running a worker is not — the worker loop
    is a coroutine. Two ``App`` objects with the *same* task registry is the
    arrangement Procrastinate's own ``with_connector`` used, and it is what
    keeps the tasks the worker runs identical to the ones the gateway deferred
    rather than a second registration that could drift.

    ``replace_connector`` is deliberately not used: it mutates the application
    it is called on, so a caller holding both would find the two are one
    object and the synchronous enqueue would start going through the
    asynchronous path.
    """
    from procrastinate import App, PsycopgConnector

    worker = App(connector=PsycopgConnector(conninfo=conninfo))
    worker.tasks = app.tasks
    return worker


def apply_queue_schema(app: App) -> None:
    """Create Procrastinate's own tables, once.

    Its ``schema.sql`` has no ``IF NOT EXISTS`` guards, so this is a
    once-per-database operation and a second call raises. That is the library
    being honest about owning its own DDL; this card neither copies that SQL nor
    extends it — it asks the library to apply it, through the library's own
    ``SchemaManager``. The application has to be open first, because the
    manager runs the statements over the connector's pool.
    """
    app.schema_manager.apply_schema()


def stage_task_name(stage: Stage) -> str:
    return f"{STAGE_TASK_PREFIX}{stage.value}"


def stage_queueing_lock(job_id: UUID, stage: Stage) -> str:
    """The key that makes a stage enqueue exactly once while it is waiting.

    Procrastinate enforces this with a partial unique index on
    ``queueing_lock`` over rows in ``todo``, so it holds under concurrency and
    not merely under a read-then-write in this module. A crash between the
    canonical job row and its message is therefore visible as a refusal, which
    is what :class:`TaskAlreadyEnqueued` is for.
    """
    return f"job:{job_id}:{stage.value}"


def stage_retry() -> Any:
    """The bounded retry strategy every stage task uses.

    Only genuine, transient failures are retried. A paused quota, an
    unsupported scan and a lost lease are excluded by construction — they are
    raised as their own exception types, none of which is ``StageFailure``, and
    the exception type is therefore the retry policy rather than a comment
    about it.
    """
    from procrastinate import RetryStrategy

    from kb.processing.worker import StageFailure

    return RetryStrategy(
        max_attempts=MAX_ATTEMPTS,
        exponential_wait=2,
        retry_exceptions=[StageFailure],
    )


def enqueue_stages(
    app: App,
    conn: psycopg.Connection,
    *,
    job_id: UUID,
    stages: tuple[Stage, ...],
) -> list[int]:
    """Queue every stage of ``job_id`` on ``conn``, inside ``conn``'s transaction.

    ``conn`` is the caller's own connection, so the queue rows and whatever
    else that transaction is writing commit or roll back together. Nothing is
    deferred on a second connection behind the caller's back, which is the
    failure mode an outbox exists to prevent and which this arrangement simply
    does not have.
    """
    queued: list[int] = []
    for stage in stages:
        task = app.tasks[stage_task_name(stage)]
        try:
            queued.append(
                task.configure(
                    connection=conn,
                    queue=QUEUE_FOR_STAGE[stage],
                    queueing_lock=stage_queueing_lock(job_id, stage),
                ).defer(job_id=str(job_id))
            )
        except Exception as exc:  # procrastinate.exceptions.AlreadyEnqueued
            if type(exc).__name__ != "AlreadyEnqueued":
                raise
            raise TaskAlreadyEnqueued(
                f"stage {stage.value} of job {job_id} is already waiting in the queue"
            ) from exc
    return queued


def _stage_task_names() -> tuple[str, ...]:
    return tuple(stage_task_name(stage) for stage in QUEUE_FOR_STAGE)


def completed_stages(conn: psycopg.Connection, job_id: UUID) -> frozenset[Stage]:
    """Stages of ``job_id`` whose queue task reached ``succeeded``.

    Read with a single statement over the library's own bookkeeping table. It
    takes no lock, so a UI asking "which stage is this at" never waits behind a
    stage that is running.
    """
    cur = conn.execute(
        "SELECT task_name FROM procrastinate_jobs "
        "WHERE task_name = ANY(%s) AND status = 'succeeded' AND args ->> 'job_id' = %s",
        (list(_stage_task_names()), str(job_id)),
    )
    done = set()
    for (task_name,) in cur.fetchall():
        done.add(Stage(task_name[len(STAGE_TASK_PREFIX) :]))
    return frozenset(done)


def task_attempts(conn: psycopg.Connection, job_id: UUID) -> int:
    """How many times any stage of this job has been dequeued for execution.

    This is the retry counter the catalogue schema does not have: Procrastinate
    keeps it, increments it on every fetch, and the bounded budget in
    :data:`MAX_ATTEMPTS` is measured against it.
    """
    cur = conn.execute(
        "SELECT COALESCE(max(attempts), 0) FROM procrastinate_jobs "
        "WHERE task_name = ANY(%s) AND args ->> 'job_id' = %s",
        (list(_stage_task_names()), str(job_id)),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def quota_paused_message(stage: Stage, *, generation_allowed: bool) -> str | None:
    """Why a stage is not running, or ``None`` when it may.

    A paused quota is a policy answer, not an error, and the reason is spelled
    out so the operator sees it in the job's own record instead of an
    indefinite spinner.
    """
    if generation_allowed or stage not in QUOTA_GATED_STAGES:
        return None
    return "generative stages are not enabled for this library; reading and search are unaffected"
