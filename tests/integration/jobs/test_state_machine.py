"""C13 — the closed vocabulary and the state machine, against a real server.

Two claims are checked here and neither is checkable by reading the code:

* an unknown status is an error, not a free string that drifts between the
  worker, the API and the database; and
* the machine has no edge that would let a finished job go back to work, or
  let ``needs_ocr`` — a real answer about an unsupported scan — be retried
  forever.
"""

from __future__ import annotations

import pytest

from kb.contracts.enums import ProcessingStatus
from kb.processing import jobs as jobs_domain
from kb.processing.jobs import JobKind, UnknownJobKind, UnknownJobStatus
from kb.processing.stages import StageOutcome, terminal_outcome

pytestmark = pytest.mark.integration


# ------------------------------------------------------- the closed vocabulary


@pytest.mark.parametrize(
    "value",
    ["queued", "running", "partial", "needs_ocr", "failed", "done"],
)
def test_every_status_in_the_database_enum_is_known_to_the_code(value: str) -> None:
    assert jobs_domain.parse_status(value) is ProcessingStatus(value)


@pytest.mark.parametrize(
    "value",
    ["", "QUEUED", "Done", "in_progress", "needs-ocr", "paused", "queued ", None, 0, True],
)
def test_anything_outside_the_enum_is_an_error_not_a_string(value: object) -> None:
    with pytest.raises(UnknownJobStatus):
        jobs_domain.parse_status(value)


def test_the_enum_in_the_database_and_the_enum_in_the_code_are_the_same_set(world) -> None:
    """A status the schema allows but the code does not would be a silent gap."""
    rows = world.conn.execute(
        "SELECT enumlabel FROM pg_enum "
        "JOIN pg_type ON pg_type.oid = pg_enum.enumtypid "
        "WHERE typname = 'processing_status'"
    ).fetchall()
    in_database = {str(label) for (label,) in rows}
    in_code = {status.value for status in ProcessingStatus}
    assert in_database == in_code


def test_an_unknown_job_kind_is_an_error_too() -> None:
    with pytest.raises(UnknownJobKind):
        jobs_domain.parse_kind("transcribe")
    assert jobs_domain.parse_kind("ingest") is JobKind.INGEST


def test_a_status_outside_the_enum_cannot_be_stored_at_all(world, library) -> None:
    """The database is the second line of defence, and it holds."""
    from psycopg import errors

    with pytest.raises(errors.InvalidTextRepresentation):
        world.conn.execute(
            "UPDATE kb.job SET status = 'paused_quota' WHERE library_id = %s", (library,)
        )


# ------------------------------------------------------------ the state machine


def test_terminal_statuses_are_done_and_needs_ocr_and_nothing_else() -> None:
    assert jobs_domain.TERMINAL_STATUSES == frozenset(
        {ProcessingStatus.DONE, ProcessingStatus.NEEDS_OCR}
    )


def test_needs_ocr_is_terminal_for_the_worker() -> None:
    assert jobs_domain.is_terminal(ProcessingStatus.NEEDS_OCR)
    assert jobs_domain.ALLOWED_TRANSITIONS[ProcessingStatus.NEEDS_OCR] == frozenset()


def test_a_finished_job_cannot_go_back_to_work() -> None:
    assert jobs_domain.ALLOWED_TRANSITIONS[ProcessingStatus.DONE] == frozenset()
    for target in ProcessingStatus:
        assert not jobs_domain.may_transition(ProcessingStatus.DONE, target)


def test_a_job_cannot_skip_straight_from_queued_to_done() -> None:
    assert not jobs_domain.may_transition(ProcessingStatus.QUEUED, ProcessingStatus.DONE)
    assert jobs_domain.may_transition(ProcessingStatus.QUEUED, ProcessingStatus.RUNNING)


def test_every_status_has_a_declared_row_in_the_machine() -> None:
    assert set(jobs_domain.ALLOWED_TRANSITIONS) == set(ProcessingStatus)


def test_partial_resumes_rather_than_restarts() -> None:
    assert jobs_domain.may_transition(ProcessingStatus.PARTIAL, ProcessingStatus.RUNNING)
    assert jobs_domain.may_transition(ProcessingStatus.PARTIAL, ProcessingStatus.DONE)


def test_a_failed_job_is_re_armed_by_an_explicit_requeue() -> None:
    assert jobs_domain.may_transition(ProcessingStatus.FAILED, ProcessingStatus.QUEUED)
    assert not jobs_domain.may_transition(ProcessingStatus.FAILED, ProcessingStatus.RUNNING)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ProcessingStatus.DONE, StageOutcome.COMMITTED),
        (ProcessingStatus.NEEDS_OCR, StageOutcome.NEEDS_OCR),
        (ProcessingStatus.FAILED, StageOutcome.FAILED_RETRYABLE),
        (ProcessingStatus.PARTIAL, StageOutcome.COMMITTED),
        (ProcessingStatus.RUNNING, None),
        (ProcessingStatus.QUEUED, None),
    ],
)
def test_each_status_maps_onto_what_it_means_for_the_ladder(
    status: ProcessingStatus, expected: StageOutcome | None
) -> None:
    assert terminal_outcome(status) is expected


def test_needs_ocr_and_a_paused_quota_are_not_failures() -> None:
    """Neither may become a retry: both are answers, not faults."""
    assert StageOutcome.NEEDS_OCR.is_success
    assert StageOutcome.PAUSED.is_success
    assert not StageOutcome.FAILED_RETRYABLE.is_success
    assert not StageOutcome.FAILED_FINAL.is_success
