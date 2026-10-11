"""C13 — the stage ladder and the resume rule.

These cases are pure: no database, no queue, no server. They are here because
the ladder is the one piece of this card whose bug would be invisible from the
outside — a resume that starts at the wrong stage still produces a ``done``
job, it just produces it wrongly and slowly.

The ordering case at the end is deliberately separate from the rest for a
reason recorded in the card's result file: the ladder's *order* is currently
pinned by these tests and not by the end-to-end run, because a single worker
serving every queue already fetches tasks in the order they were enqueued.
"""

from __future__ import annotations

import pytest

from kb.processing.jobs import JobKind
from kb.processing.stages import (
    LADDER,
    QUOTA_GATED_STAGES,
    Stage,
    StageOutcome,
    first_pending,
    ladder_for,
    remaining,
    terminal_outcome,
)

pytestmark = pytest.mark.integration


def test_every_job_kind_has_a_ladder_and_no_ladder_is_empty() -> None:
    assert set(LADDER) == set(JobKind)
    for kind, ladder in LADDER.items():
        assert ladder, kind
        assert len(set(ladder)) == len(ladder), kind


def test_no_stage_appears_twice_in_a_ladder() -> None:
    for kind, ladder in LADDER.items():
        assert len(set(ladder)) == len(ladder), kind


def test_every_stage_is_reachable_from_some_ladder() -> None:
    reachable = {stage for ladder in LADDER.values() for stage in ladder}
    assert reachable == set(Stage)


def test_the_ladder_of_a_kind_is_the_ladder_of_that_kind() -> None:
    for kind in JobKind:
        assert ladder_for(kind) == LADDER[kind]


def test_only_the_cloud_stage_spends_the_subscription() -> None:
    assert QUOTA_GATED_STAGES == {Stage.EXTRACT_CLOUD}
    for kind, ladder in LADDER.items():
        gated = [stage for stage in ladder if stage in QUOTA_GATED_STAGES]
        # An ingest must never be able to spend the owner's quota by accident.
        assert gated == ([] if kind is JobKind.INGEST else gated), kind


def test_remaining_keeps_the_ladder_order_not_the_caller_s_order() -> None:
    ladder = ladder_for(JobKind.INGEST)
    assert remaining(ladder, frozenset({Stage.INDEX})) == (
        Stage.PARSE,
        Stage.EMBED,
        Stage.MAINTAIN,
    )
    # A caller that hands over a set gets the ladder's order back, not a set's.
    assert remaining(ladder, frozenset({Stage.MAINTAIN, Stage.PARSE})) == (
        Stage.EMBED,
        Stage.INDEX,
    )


def test_a_finished_ladder_has_nothing_pending() -> None:
    ladder = ladder_for(JobKind.INGEST)
    assert remaining(ladder, frozenset(ladder)) == ()
    assert first_pending(ladder, frozenset(ladder)) is None


def test_an_untouched_ladder_resumes_at_the_first_stage() -> None:
    assert first_pending(ladder_for(JobKind.INGEST), frozenset()) is Stage.PARSE


def test_a_resume_starts_at_the_first_gap_not_at_the_end() -> None:
    """The bug this whole function exists to prevent.

    Picking "the last stage that ran" or "the last one left" rebuilds an index
    from fragments that were never parsed, and it looks fine from the outside:
    the job still ends up ``done``.
    """
    ladder = ladder_for(JobKind.INGEST)
    after_parse = frozenset({Stage.PARSE})
    assert first_pending(ladder, after_parse) is Stage.EMBED
    assert remaining(ladder, after_parse)[0] is Stage.EMBED

    after_two = frozenset({Stage.PARSE, Stage.EMBED})
    assert first_pending(ladder, after_two) is Stage.INDEX
    assert remaining(ladder, after_two)[0] is Stage.INDEX

    # And specifically: with three stages left, the answer is the first of the
    # three. A mutation to last_pending() would change this line and nothing
    # else in the file, which is why it is asserted on its own.
    three_left = frozenset({Stage.PARSE})
    assert len(remaining(ladder_for(JobKind.INGEST), three_left)) == 3
    assert first_pending(ladder_for(JobKind.INGEST), three_left) is Stage.EMBED
    del after_two


def test_a_partial_ladder_resumes_at_its_own_first_gap() -> None:
    ladder = ladder_for(JobKind.EXTRACT)
    assert first_pending(ladder, frozenset({Stage.INDEX})) is Stage.EXTRACT_CLOUD
    assert first_pending(ladder, frozenset({Stage.EXTRACT_CLOUD})) is Stage.INDEX


def test_stages_outside_the_ladder_are_simply_not_pending() -> None:
    """A completed stage of a different kind must not confuse a resume."""
    ladder = ladder_for(JobKind.INGEST)
    assert first_pending(ladder, frozenset({Stage.EXTRACT_CLOUD})) is Stage.PARSE


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("done", StageOutcome.COMMITTED),
        ("needs_ocr", StageOutcome.NEEDS_OCR),
        ("failed", StageOutcome.FAILED_RETRYABLE),
        ("partial", StageOutcome.COMMITTED),
        ("running", None),
        ("queued", None),
    ],
)
def test_a_terminal_status_stops_the_ladder_and_a_transient_one_does_not(
    status: str, expected: StageOutcome | None
) -> None:
    from kb.contracts.enums import ProcessingStatus

    assert terminal_outcome(ProcessingStatus(status)) is expected
