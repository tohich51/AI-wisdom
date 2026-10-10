"""C15 — the generation policy, on a real PostgreSQL 16.2.

Three decisions, each with a database constraint underneath it:

* **opt-in** — ``kb.generation_policy`` says no until a manager says yes;
* **owner-started** — a colleague asking is not permission to spend the
  owner's subscription;
* **one at a time** — installation-wide, enforced by a partial unique index,
  so two racing callers collide rather than both winning.

And the card's other deliverable: **a half-finished reindex is detectable
through the generation registry**. That is
``test_a_half_finished_reindex_is_detectable`` below, and it is the test that
would have caught a rebuild that died with the library still reporting itself
as searchable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from kb.retrieval.provisioning_generations import (
    GenerationRefused,
    SetGenerationPolicy,
    admit_rebuild,
    fail_generation,
    list_generations,
    publish_generation,
    read_generation_policy,
    read_index_status,
    set_generation_policy,
)

pytestmark = pytest.mark.integration

CONTENT = "a" * 64
OTHER_CONTENT = "b" * 64


# ------------------------------------------------------------------- opt-in


def test_a_library_with_no_policy_row_cannot_generate(gateway, world):
    """No row is not a default of false; it is a default of deny.

    The same answer, arrived at by the insert policy's EXISTS clause, which
    finds no opt-in to satisfy.
    """
    library, _people = world.library_with(("owner", "manager"))
    with pytest.raises(GenerationRefused):
        admit_rebuild(gateway, world.manager, library, CONTENT)


def test_generation_is_off_until_the_owner_turns_it_on(gateway, world):
    library, _people = world.library_with(("owner", "manager"))
    written = set_generation_policy(
        gateway, world.manager, library, SetGenerationPolicy(generation_allowed=False)
    )
    assert written.generation_allowed is False
    with pytest.raises(GenerationRefused):
        admit_rebuild(gateway, world.manager, library, CONTENT)

    set_generation_policy(
        gateway, world.manager, library, SetGenerationPolicy(generation_allowed=True)
    )
    assert admit_rebuild(gateway, world.manager, library, CONTENT).generation == 1


def test_only_a_manager_may_opt_a_library_in(gateway, world):
    """A curator's write is *filtered*, not rejected — so zero rows is a refusal.

    This is the C06 shape again: an UPDATE under RLS reports ``UPDATE 0`` and
    raises nothing. Reading a row count as success is how a policy that was
    never satisfied ends up believed.
    """
    library, _people = world.library_with(("owner", "manager"), ("colleague", "curator"))
    with pytest.raises(GenerationRefused):
        set_generation_policy(
            gateway,
            world.principal(world.colleague),
            library,
            SetGenerationPolicy(generation_allowed=True),
        )
    assert read_generation_policy(gateway, world.manager, library) is None


def test_a_colleagues_request_is_not_a_permission_to_spend_the_quota(gateway, world):
    """PRODUCT-SPEC: "Запрос коллеги не означает разрешение тратить подписку владельца"."""
    library, _people = world.library_with(("owner", "manager"), ("colleague", "curator"))
    world.generation_policy(library, allowed=True, requires_owner_start=True)
    with pytest.raises(GenerationRefused):
        admit_rebuild(gateway, world.principal(world.colleague), library, CONTENT)

    # When the library says a curator may start it, the same curator may. The
    # difference is a policy the owner set, not a role the database invented.
    world.generation_policy(library, allowed=True, requires_owner_start=False)
    assert (
        admit_rebuild(gateway, world.principal(world.colleague), library, CONTENT).generation == 1
    )


def test_concurrency_greater_than_one_is_refused_at_the_model(gateway, world):
    """A column that promises 3 while a constraint allows 1 is a lie."""
    with pytest.raises(ValidationError, match="concurrency is fixed at 1"):
        SetGenerationPolicy(generation_allowed=True, max_concurrency=3)
    library, _people = world.library_with(("owner", "manager"))
    written = set_generation_policy(
        gateway,
        world.manager,
        library,
        SetGenerationPolicy(generation_allowed=True, max_concurrency=1),
    )
    assert written.max_concurrency == 1


# ----------------------------------------------------------------- one slot


def test_generation_runs_one_at_a_time_for_the_whole_installation(gateway, world):
    """Two libraries, two managers, one slot. The second is refused.

    A partial unique index over the constant ``'building'`` is the whole
    mechanism. An application that forgot to take a lock still cannot get two.
    """
    first, _p1 = world.library_with(("owner", "manager"))
    second, _p2 = world.library_with(("owner", "manager"))
    world.generation_policy(first)
    world.generation_policy(second)

    assert admit_rebuild(gateway, world.manager, first, CONTENT).generation == 1
    with pytest.raises(GenerationRefused) as caught:
        admit_rebuild(gateway, world.manager, second, OTHER_CONTENT)
    assert "single generation slot" in str(caught.value)

    # And when the first finishes, the second may go.
    publish_generation(gateway, world.manager, first, 1)
    assert admit_rebuild(gateway, world.manager, second, OTHER_CONTENT).generation == 1


def test_the_same_content_does_not_create_a_second_generation(gateway, world):
    """Idempotent by content: a re-run resumes rather than forks."""
    library, _people = world.library_with(("owner", "manager"))
    world.generation_policy(library)

    first = admit_rebuild(gateway, world.manager, library, CONTENT)
    second = admit_rebuild(gateway, world.manager, library, CONTENT)
    assert first.generation == second.generation
    assert first.resumed is False
    assert second.resumed is True
    assert len(list_generations(gateway, world.manager, library)) == 1


def test_a_failed_rebuild_is_a_new_attempt_not_a_resurrection(gateway, world):
    """A dead generation cannot be reopened; the retry gets its own number.

    Otherwise the journal would show generation 2 succeeding with no record
    that generation 2 ever failed, and "which attempt is this" would have no
    answer.
    """
    library, _people = world.library_with(("owner", "manager"))
    world.generation_policy(library)

    admit_rebuild(gateway, world.manager, library, CONTENT)
    fail_generation(gateway, world.manager, library, 1, "embedder process died")

    retry = admit_rebuild(gateway, world.manager, library, CONTENT)
    assert retry.generation == 2
    assert retry.resumed is False
    rows = list_generations(gateway, world.manager, library)
    failed = [r for r in rows if r["generation"] == 1]
    assert failed[0]["state"] == "retired"
    assert failed[0]["failure_reason"] == "embedder process died"


def test_a_failed_generation_needs_a_reason(gateway, world):
    library, _people = world.library_with(("owner", "manager"))
    world.generation_policy(library)
    admit_rebuild(gateway, world.manager, library, CONTENT)
    with pytest.raises(GenerationRefused):
        fail_generation(gateway, world.manager, library, 1, "   ")


def test_a_retired_generation_cannot_be_published(gateway, world):
    library, _people = world.library_with(("owner", "manager"))
    world.generation_policy(library)
    admit_rebuild(gateway, world.manager, library, CONTENT)
    fail_generation(gateway, world.manager, library, 1, "died")
    with pytest.raises(GenerationRefused):
        publish_generation(gateway, world.manager, library, 1)


# ------------------------------------------- the half-finished reindex test


def test_a_half_finished_reindex_is_detectable(gateway, world):
    """The deliverable: a build that never finished is visible, not silent.

    This is the case that matters operationally. A rebuild is admitted, the
    job dies mid-flight, and nothing ever moves the row. Before this card, a
    boolean "index ready" would still say yes and the library would keep
    serving text from a projection nobody is maintaining. The registry says
    three separate things: which generation is current, which is building,
    and whether the two match the library's content.
    """
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.insert_account(library, state="ready")
    world.generation_policy(library)
    world.published_generation(library, 1)

    # steady state
    before = read_index_status(gateway, world.reader, library)
    assert before is not None
    assert before.index_ready is True
    assert before.building_generation is None

    # the content moves and a rebuild is admitted
    world.bump_content_generation(library, 2)
    admit_rebuild(gateway, world.manager, library, "c" * 64)

    # the job dies. nothing else happens: no publish, no fail.
    mid = read_index_status(gateway, world.reader, library)
    assert mid is not None
    assert mid.building_generation == 2, "an open build is invisible"
    assert mid.current_generation == 1
    assert mid.library_generation == 2
    assert mid.index_ready is False, "a library mid-rebuild reported itself searchable"
    assert mid.skip_reason() == "rebuild_in_progress"

    # the operator can see the row, its attempt, and who started it
    rows = list_generations(gateway, world.manager, library)
    stuck = [r for r in rows if r["generation"] == 2]
    assert stuck[0]["state"] == "building"
    assert stuck[0]["finished_at"] is None
    assert stuck[0]["started_by"] == world.owner
    assert stuck[0]["canary_uri"] is None, "an unverified value stays None, not a placeholder"

    # and after the operator fails it, the library goes back to serving the
    # generation it actually has — while the content gap is still reported.
    fail_generation(gateway, world.manager, library, 2, "worker killed, no ack")
    after = read_index_status(gateway, world.reader, library)
    assert after is not None
    assert after.building_generation is None
    assert after.index_ready is False
    assert after.skip_reason() == "index_behind"


def test_a_stale_index_is_not_served(gateway, world):
    """A11: withdrawn content must not be answered from an old projection."""
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.insert_account(library, state="ready")
    world.generation_policy(library)
    world.published_generation(library, 1)
    world.bump_content_generation(library, 2)

    stale = read_index_status(gateway, world.reader, library)
    assert stale is not None
    assert stale.index_ready is False
    assert stale.skip_reason() == "index_behind"

    admit_rebuild(gateway, world.manager, library, "d" * 64)
    publish_generation(gateway, world.manager, library, 2)
    fresh = read_index_status(gateway, world.reader, library)
    assert fresh is not None
    assert fresh.index_ready is True
    assert fresh.current_generation == 2


def test_a_library_that_has_never_been_indexed_is_not_ready(gateway, world):
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.insert_account(library, state="ready")
    status = read_index_status(gateway, world.reader, library)
    assert status is not None
    assert status.current_generation is None
    assert status.index_ready is False
    assert status.skip_reason() == "index_behind"


def test_publishing_retires_the_previous_generation(gateway, world):
    """Two updates, one transaction, and an index that says so."""
    library, _people = world.library_with(("owner", "manager"))
    world.generation_policy(library)
    admit_rebuild(gateway, world.manager, library, CONTENT)
    publish_generation(gateway, world.manager, library, 1, canary_uri="canary/v1")
    world.bump_content_generation(library, 2)
    admit_rebuild(gateway, world.manager, library, "e" * 64)
    publish_generation(gateway, world.manager, library, 2, canary_uri="canary/v2")

    rows = list_generations(gateway, world.manager, library)
    states = {r["generation"]: r["state"] for r in rows}
    assert states == {1: "retired", 2: "current"}
    assert read_index_status(gateway, world.manager, library).index_ready is True


def test_the_status_function_tells_a_stranger_nothing(gateway, world):
    """A SECURITY DEFINER function must not become an existence oracle (A20)."""
    library, _people = world.library_with(("owner", "manager"))
    world.published_generation(library, 1)
    assert read_index_status(gateway, world.nobody, library) is None
    # And the same answer as for a library that does not exist.
    missing = uuid4()
    assert read_index_status(gateway, world.nobody, missing) is None


def test_a_reader_cannot_read_the_registry_itself(gateway, world):
    """The four numbers are readable; the operational rows are not.

    ``index_generation_read`` needs contributor. A reader who could list the
    registry would learn who started a rebuild and which canary it wrote.
    """
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.generation_policy(library)
    admit_rebuild(gateway, world.manager, library, CONTENT)
    assert list_generations(gateway, world.manager, library)
    # A SELECT under RLS is FILTERED, not rejected: the reader gets an empty
    # list and no error, which is exactly the silent shape C06 warns about.
    # The assertion is therefore on the emptiness, with the manager's
    # non-empty list above as the control that the rows really exist.
    assert list_generations(gateway, world.reader, library) == []
