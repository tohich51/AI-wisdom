"""C13 — the stage ladder, and the rule that decides where a resume starts.

Five stages, named by the card: ``parse``, ``embed``, ``extract_cloud``,
``index``, ``maintenance``. Which of them a job runs is fixed by its kind, so
the ladder is data rather than a chain of ``if`` statements scattered through
the worker.

**Where the checkpoint is, and where it is not.** ``kb.job.status`` is a
single column with six values, and a five-stage pipeline does not fit in six
statuses without inventing states the ``processing_status`` enum does not have.
The per-stage checkpoint is therefore the queue row: each stage is its own
deferred task, the stage is recorded as done when Procrastinate marks that
task ``succeeded``, and that write is committed only after the stage's own
guarded write has committed. See ``kb.processing.jobs_queue`` for the read
side and ``docs/handoff/results/C13.json`` for the gap this leaves in the
catalogue schema.

This module is pure: no database, no queue, no import of Procrastinate. The
resume rule is a function of ``(ladder, completed)`` and is unit-testable
without a server, which is where a resume bug is cheapest to catch.
"""

from __future__ import annotations

from enum import StrEnum

from kb.contracts.enums import ProcessingStatus
from kb.processing.jobs import JobKind

__all__ = [
    "LADDER",
    "QUOTA_GATED_STAGES",
    "Stage",
    "StageOutcome",
    "first_pending",
    "ladder_for",
    "remaining",
    "terminal_outcome",
]


class Stage(StrEnum):
    """The processing stages. Closed, for the same reason every vocabulary is."""

    PARSE = "parse"
    EMBED = "embed"
    EXTRACT_CLOUD = "extract_cloud"
    INDEX = "index"
    MAINTAIN = "maintain"


#: Stages that spend the owner's model subscription. They are gated by
#: ``kb.generation_policy`` and are the reason "paused quota must not occupy a
#: CPU slot" is a rule rather than a hope: a worker that fetched a paused
#: cloud task and then sat on it would hold a slot for nothing.
QUOTA_GATED_STAGES: frozenset[Stage] = frozenset({Stage.EXTRACT_CLOUD})


#: What each kind of job runs, in order. A kind that appears nowhere in the
#: ladder would be a job that does nothing, so every member of ``JobKind`` is
#: present and the test suite checks that.
LADDER: dict[JobKind, tuple[Stage, ...]] = {
    # Upload → original → fragments → vectors → index → housekeeping.
    JobKind.INGEST: (Stage.PARSE, Stage.EMBED, Stage.INDEX, Stage.MAINTAIN),
    # Pulling knowledge or rules out of already-stored sources.
    JobKind.EXTRACT: (Stage.EXTRACT_CLOUD, Stage.INDEX, Stage.MAINTAIN),
    # Vectors only.
    JobKind.EMBED: (Stage.EMBED, Stage.INDEX),
    # Assembling a reviewable package.
    JobKind.COMPILE: (Stage.EXTRACT_CLOUD, Stage.INDEX),
    # Writing an export out.
    JobKind.EXPORT: (Stage.MAINTAIN,),
}


class StageOutcome(StrEnum):
    """What a stage decided.

    ``PAUSED`` and ``NEEDS_OCR`` are not failures. Both end the task
    successfully and both leave the queue alone, so neither can become a retry
    loop: a paused quota and an unsupported scan are answers, and answering
    them again immediately would only repeat the same answer.
    """

    COMMITTED = "committed"
    NEEDS_OCR = "needs_ocr"
    PAUSED = "paused"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"

    @property
    def is_success(self) -> bool:
        """True when the task itself should finish without raising.

        ``PAUSED`` is here on purpose. A paused quota releases the worker's
        concurrency slot immediately; re-deferring inside the task would hold
        the slot and spin.
        """
        return self in _SUCCESSFUL_OUTCOMES


_SUCCESSFUL_OUTCOMES = frozenset(
    {StageOutcome.COMMITTED, StageOutcome.PAUSED, StageOutcome.NEEDS_OCR}
)


def ladder_for(kind: JobKind) -> tuple[Stage, ...]:
    """The stages one kind of job runs. Every kind has at least one."""
    return LADDER[kind]


def remaining(ladder: tuple[Stage, ...], completed: frozenset[Stage]) -> tuple[Stage, ...]:
    """Stages of ``ladder`` that are not in ``completed``, in ladder order.

    Order is the ladder's, not the caller's: a resume that ran ``index``
    before ``parse`` would be a bug, and taking the order from the data
    removes the opportunity to get it wrong.
    """
    return tuple(stage for stage in ladder if stage not in completed)


def first_pending(ladder: tuple[Stage, ...], completed: frozenset[Stage]) -> Stage | None:
    """The single stage a restart should resume at, or ``None`` if finished.

    A resume starts at the *first* unfinished stage, not at the last one that
    ran. Running stages out of order would rebuild an index from fragments
    that were never parsed, and picking "the last one" is exactly the bug that
    makes a restart double work instead of repeating it.
    """
    pending = remaining(ladder, completed)
    return pending[0] if pending else None


def terminal_outcome(status: ProcessingStatus) -> StageOutcome | None:
    """Map a job's coarse status onto what it means for the ladder.

    ``needs_ocr`` and ``done`` are terminal: no stage will ever run again
    against that job without a new submission under a new idempotency key.
    ``failed`` and ``partial`` are not — they are where a restart resumes.
    """
    if status is ProcessingStatus.DONE:
        return StageOutcome.COMMITTED
    if status is ProcessingStatus.NEEDS_OCR:
        return StageOutcome.NEEDS_OCR
    if status is ProcessingStatus.FAILED:
        return StageOutcome.FAILED_RETRYABLE
    if status is ProcessingStatus.PARTIAL:
        return StageOutcome.COMMITTED
    return None
