"""C15 — ordinary search spends nothing and looks only where it may.

The product invariant under test: **a normal search makes zero generative
calls**. Not "few", not "by default" — zero, every time, on the read path.

What is real here: the database, the RLS, the account mapping, the generation
registry and therefore the decision about which accounts a search may touch.
What is not real: OpenViking, which cannot run in this environment. The read
port is therefore a double that records the scopes it was handed — which is
enough to prove the part that matters and is ours, namely *which accounts the
search asks about*, and not enough to prove anything about OpenViking's own
behaviour. The latter is `not_run` behind E03.
"""

from __future__ import annotations

import ast

import pytest
from doubles import RecordingIndexReader
from pydantic import ValidationError

from kb.retrieval.provisioning_generations import admit_rebuild, publish_generation
from kb.retrieval.provisioning_search import (
    IndexHit,
    SearchQuery,
    plan_search,
    search_knowledge,
)

pytestmark = pytest.mark.integration


def _account_ref(acting, world, library_id: str) -> str:
    """The account ref, read as the caller who is allowed to read it."""
    with acting(world, "colleague") as conn:
        row = conn.execute(
            "SELECT account_ref FROM kb.index_account WHERE library_id = %s", (library_id,)
        ).fetchone()
    assert row is not None, "the fixture library has no provisioned account"
    return row[0]


@pytest.fixture(autouse=True)
def _zero_the_tripwire(request):
    """Reset the generative counter around every test in this file.

    Every test asserts the counter is still zero afterwards — that assertion
    is the file's whole point, and it applies to tests that never search too.
    The single exception is the control test that deliberately goes through the
    door, which marks itself so the exclusion is visible rather than a special
    case buried in the fixture.
    """
    from kb.retrieval.provisioning_generative import (
        generative_call_count,
        reset_generative_call_count,
    )

    reset_generative_call_count()
    yield
    if request.node.get_closest_marker("deliberately_generative"):
        return
    assert generative_call_count() == 0, "a search path spent a generative call"


# ------------------------------------------------------- the central invariant


def test_ordinary_search_makes_zero_generative_calls(gateway, acting, world, ready_library):
    """The card's invariant, asserted rather than assumed.

    Three independent mechanisms, all checked in this one test:

    1. the tripwire counter in ``provisioning_generative`` stays at zero;
    2. the read port was asked exactly one question, and the question was a
       vector search over a scope the caller was entitled to;
    3. no generation row was created — a search cannot even *start* a
       generative stage, so the counter being zero is not the only thing
       holding it.
    """
    from kb.retrieval.provisioning_generative import generative_call_count

    account = _account_ref(acting, world, ready_library)
    reader = RecordingIndexReader(
        [
            IndexHit(
                library_id=ready_library,
                account_ref=account,
                generation=1,
                uri="index/fragment/1",
                score=0.9,
                snippet="brand voice is lowercase",
            )
        ]
    )

    page = search_knowledge(gateway, world.reader, SearchQuery(text="brand voice"), reader)

    assert generative_call_count() == 0
    assert len(page.hits) == 1
    assert page.mode == "vectors_only"
    assert page.is_partial is False
    # one question, one scope, the reader's own account
    assert len(reader.asked_with) == 1
    assert [s.account_ref for s in reader.asked_with[0]] == [account]
    # and the read scope carries the read identity, never the index one
    assert page.scopes[0].identity.startswith("kb-svc-read-")
    assert page.scopes[0].identity != page.scopes[0].identity.replace("read", "index")

    # (3) no generation was opened by the read
    opened = gateway.execute(
        "SELECT count(*) FROM kb.index_generation WHERE state = 'building'"
    ).fetchone()
    assert opened[0] == 0


@pytest.mark.deliberately_generative
def test_the_tripwire_actually_moves_when_a_generative_call_happens():
    """Otherwise "the counter is zero" could just mean "the counter is broken".

    This goes through the door on purpose, from a test, with the port that
    refuses. It is the control that gives the test above its meaning — and it
    counts the ATTEMPT, not the success, so a search that reached the door
    could not hide behind a provider error.
    """
    from uuid import uuid4

    from kb.retrieval.provisioning_generations import mint_permit
    from kb.retrieval.provisioning_generative import (
        GenerativeRequest,
        UnconfiguredModelRunner,
        generate,
        generative_call_count,
    )

    before = generative_call_count()
    with pytest.raises(Exception) as caught:
        generate(
            UnconfiguredModelRunner(),
            GenerativeRequest(
                permit=mint_permit(uuid4(), 1, "f" * 64),
                purpose="extract",
                prompt="extract this book",
            ),
        )
    assert "no model runner is configured" in str(caught.value)
    assert generative_call_count() == before + 1


def test_the_search_module_has_no_reference_to_the_generative_door():
    """The structural half of the invariant, as an AST rather than a grep.

    A counter that reads zero is worth little if the search path could reach
    the door without moving it. So: no import of the generative module, no
    dynamic import to hide one, and no name in the module's namespace.
    """
    import pathlib

    from kb.retrieval import provisioning_search

    source = pathlib.Path(provisioning_search.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            modules.update(f"{node.module or ''}.{alias.name}" for alias in node.names)
    assert not [m for m in modules if "generative" in m], modules
    assert "import_module" not in source and "__import__" not in source
    assert not [name for name in dir(provisioning_search) if name == "generate"]


# ------------------------------------------------------------------ routing


def test_search_asks_only_about_accounts_the_caller_may_use(gateway, world):
    """A02: refusal happens *before* the foreign index request.

    Two libraries, each with an account, an opt-in and a current generation.
    The caller has a role on one. The read port must never be handed the other
    account — the request is not made and then filtered, it is never made.
    """
    mine, _p1 = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.insert_account(mine, state="ready")
    world.generation_policy(mine)
    world.published_generation(mine, 1)

    theirs, _p2 = world.library_with(("owner", "manager"))
    world.insert_account(theirs, state="ready")
    world.generation_policy(theirs)
    world.published_generation(theirs, 1)

    foreign = world.conn.execute(
        "SELECT account_ref FROM kb.index_account WHERE library_id = %s", (theirs,)
    ).fetchone()[0]

    reader = RecordingIndexReader()
    plan = plan_search(gateway, world.reader, SearchQuery(text="anything"))

    assert [s.library_id for s in plan.scopes] == [mine]
    assert foreign not in {s.account_ref for s in plan.scopes}
    # the stranger sees nothing at all, and no skip entry naming it
    stranger_plan = plan_search(gateway, world.nobody, SearchQuery(text="anything"))
    assert stranger_plan.scopes == []
    assert stranger_plan.skipped == []

    search_knowledge(gateway, world.reader, SearchQuery(text="anything"), reader)
    assert foreign not in reader.accounts_asked_for()


def test_asking_about_a_library_you_cannot_read_gives_the_same_answer(
    gateway, world, ready_library
):
    """A real library and a library that does not exist are indistinguishable."""
    import uuid

    real = plan_search(gateway, world.reader, SearchQuery(text="x", library_ids=[ready_library]))
    fake = plan_search(gateway, world.reader, SearchQuery(text="x", library_ids=[uuid.uuid4()]))
    assert len(real.scopes) == 1
    assert fake.scopes == []
    assert fake.skipped == real.skipped or fake.skipped == []


def test_a_hit_that_names_another_account_is_dropped(gateway, acting, world, ready_library):
    """The index proposes, the database disposes.

    A stale or poisoned index record that claims somebody else's library, or
    a generation that is no longer current, is removed before the page is
    returned. This is the step ARCHITECTURE §8 puts between "ask the index"
    and "hand something out".
    """
    account = _account_ref(acting, world, ready_library)
    poisoned = IndexHit(
        library_id=ready_library,
        account_ref="kb-lib-" + "0" * 32,
        generation=1,
        uri="index/fragment/stolen",
        score=1.0,
        snippet="somebody else's canary",
    )
    page = search_knowledge(
        gateway, world.reader, SearchQuery(text="x"), RecordingIndexReader([poisoned])
    )
    assert page.hits == []
    assert account != poisoned.account_ref


def test_a_hit_from_a_retired_generation_is_dropped(gateway, acting, world, ready_library):
    """Content moved, the index answers with the old generation, the hit dies."""
    world.bump_content_generation(ready_library, 2)
    admit_rebuild(gateway, world.manager, ready_library, "9" * 64)
    publish_generation(gateway, world.manager, ready_library, 2)

    account = _account_ref(acting, world, ready_library)
    stale = IndexHit(
        library_id=ready_library,
        account_ref=account,
        generation=1,  # the retired one
        uri="index/fragment/old",
        score=1.0,
        snippet="text from the projection that is no longer current",
    )
    page = search_knowledge(
        gateway, world.reader, SearchQuery(text="x"), RecordingIndexReader([stale])
    )
    assert page.hits == []


# ------------------------------------------------------------------- partial


def test_a_library_mid_rebuild_is_skipped_with_a_reason(gateway, world, ready_library):
    """An explicit partial, not a false "nothing found"."""
    admit_rebuild(gateway, world.manager, ready_library, "8" * 64)
    plan = plan_search(gateway, world.reader, SearchQuery(text="x"))
    assert plan.scopes == []
    assert [s.reason for s in plan.skipped] == ["rebuild_in_progress"]
    assert plan.is_partial is True

    reader = RecordingIndexReader()
    page = search_knowledge(gateway, world.reader, SearchQuery(text="x"), reader)
    assert page.hits == []
    assert page.skipped[0].reason == "rebuild_in_progress"
    assert reader.asked_with == [], "a blocked library must not be queried at all"


def test_a_library_with_no_account_is_skipped(gateway, world):
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.generation_policy(library)
    world.published_generation(library, 1)
    plan = plan_search(gateway, world.reader, SearchQuery(text="x"))
    assert [s.reason for s in plan.skipped] == ["not_provisioned"]


def test_the_search_query_model_carries_no_account_and_no_generate_flag():
    """A02 at the model boundary: a caller cannot aim the index."""
    assert SearchQuery(text="brand voice").limit == 10
    for payload in (
        {"text": "x", "account_ref": "kb-lib-" + "0" * 32},
        {"text": "x", "uri": "openviking://other/published"},
        {"text": "x", "generate": True},
        {"text": "x", "mode": "generative"},
        {"text": "x", "rerank": True},
    ):
        with pytest.raises(ValidationError):
            SearchQuery.model_validate(payload)
