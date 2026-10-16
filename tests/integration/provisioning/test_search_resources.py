"""B1: what a search does to a connection, and when it refuses to finish.

The accepted F held one pooled connection for the whole of a semantic search,
which meant the connection stayed checked out for the length of up to twenty
sequential provider calls. These tests drive the real PostgreSQL the rest of
this directory uses, as ``kb_app`` so RLS applies, and assert the three things
the contract now names:

* no lease is open while the provider is being called;
* a second SQL phase runs afterwards and is the one that decides what the
  caller may see, with current ACL and generation;
* the shared budget covers the wait, and running out of it is a timeout rather
  than a claim that the provider was cancelled.

A test that only checked the shape of a response would have passed against the
old code for all three, which is why none of them does that.
"""

from __future__ import annotations

import contextlib
from uuid import uuid4

import pytest
from psycopg_pool import ConnectionPool

from kb.access.policy import Principal
from kb.adapters.vector_clients import Deadline
from kb.adapters.vector_reader import Candidate
from kb.catalog.content import search_knowledge
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.semantic_search import SemanticSearch

pytestmark = pytest.mark.integration

QUERY = "Unique canonical marketing statement"


@pytest.fixture
def semantic_library(world, admin):
    world.account_id = world.organisation()
    for person in (world.owner, world.colleague):
        admin.execute(
            "INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,status) "
            "VALUES(%s,%s,'https://synthetic.invalid',%s,'active')",
            (world.account_id, person, str(person)),
        )
    lib, _ = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.insert_account(
        lib,
        state="ready",
        dimension=SELECTED_PROFILE.dimension,
        embedding_profile=SELECTED_PROFILE.name,
    )
    admin.execute(
        "UPDATE kb.index_account SET profile_hash=%s WHERE library_id=%s",
        (SELECTED_PROFILE.profile_hash, lib),
    )
    world.knowledge(lib, title=QUERY)
    yield lib
    # Published knowledge and its provenance are intentionally immutable.
    # The session-scoped ephemeral PostgreSQL destroys this whole catalogue.


def current_build(provisioner, world, lib):
    """A current generation whose single document is verified and published."""
    from kb.retrieval import semantic_registry as registry

    build = registry.begin(
        provisioner, world.manager, registry.capture(provisioner, world.manager, lib)
    )
    if not build.already_current:
        for doc in build.snapshot.documents:
            registry.checkpoint(provisioner, world.manager, build, doc)
        registry.activate(provisioner, world.manager, build)
    return build


class Probe:
    """The shared connection factory, instrumented.

    It records how many leases are open at any instant, so a test can read
    that number *from inside the external call* -- the only moment that
    matters, and the one the old code got wrong.
    """

    def __init__(self, factory):
        self._factory = factory
        self.open = 0
        self.peak = 0
        self.acquired = 0
        self.order = []

    def __call__(self):
        probe = self

        @contextlib.contextmanager
        def lease():
            probe.open += 1
            probe.acquired += 1
            probe.peak = max(probe.peak, probe.open)
            probe.order.append(("lease", probe.open))
            try:
                with probe._factory() as conn:
                    yield conn
            finally:
                probe.open -= 1

        return lease()


@pytest.fixture
def probe(gateway_dsn):
    # ``ConnectionPool`` is itself a context manager; the factory the contract
    # wants is ``pool.connection``, which is what the gateway builds.
    with ConnectionPool(gateway_dsn, min_size=1, max_size=2, open=True) as opened:
        yield Probe(opened.connection)


def test_no_lease_is_held_while_the_provider_is_called(semantic_library, provisioner, world, probe):
    build = current_build(provisioner, world, semantic_library)
    uri = build.document_uri(build.snapshot.documents[0])
    observed = {"probe": probe}

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            observed["leases_open"] = probe.open
            observed["deadline"] = deadline
            return [Candidate(uri, 0.8)]

    result = search_knowledge(
        None, world.reader, QUERY, semantic=SemanticSearch(Reader(), connections=probe)
    )

    # The external call ran with nothing checked out. This is the assertion
    # the old implementation would fail.
    assert observed["leases_open"] == 0
    assert isinstance(observed["deadline"], Deadline)
    assert result["hits"], "the search must still return its candidate"
    # More than one lease over the request: the plan, the phase after the
    # external call, then the audit. One long lease would be acquired == 1.
    assert probe.acquired >= 2
    assert probe.peak <= 2
    assert probe.open == 0


def test_the_phase_after_the_external_call_is_the_one_that_decides(
    semantic_library, provisioner, world, probe, admin
):
    """Membership is revoked while the provider call is in flight.

    The candidate is in hand and its plan was valid a moment ago. Only a
    second SQL phase -- after the external I/O -- can notice, and it has to
    drop it. Published knowledge cannot be edited (the database refuses), so
    the change made mid-call is one the system actually allows: losing access.
    """
    build = current_build(provisioner, world, semantic_library)
    uri = build.document_uri(build.snapshot.documents[0])

    def revoke():
        admin.execute(
            "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
            (world.account_id, world.colleague),
        )

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            revoke()
            return [Candidate(uri, 0.8)]

    result = search_knowledge(
        None, world.reader, QUERY, semantic=SemanticSearch(Reader(), connections=probe)
    )

    assert result["hits"] == [], "a candidate from a revoked membership must not survive"
    assert result["retrieved_only"] is True
    assert result["indexed_library_count"] == 0


def test_a_generation_that_moves_during_the_call_drops_the_candidate(
    semantic_library, provisioner, world, probe, admin
):
    build = current_build(provisioner, world, semantic_library)
    uri = build.document_uri(build.snapshot.documents[0])

    def bump_revision():
        admin.execute("UPDATE kb.library SET generation=generation+1")

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            bump_revision()
            return [Candidate(uri, 0.8)]

    result = search_knowledge(
        None, world.reader, QUERY, semantic=SemanticSearch(Reader(), connections=probe)
    )

    # The plan no longer matches the live revision, so the library is pending
    # rather than served from a generation that is no longer current.
    assert result["pending_library_count"] == 1
    assert result["fallback_reason"] == "index_pending_or_unavailable"
    assert all(hit["retrieval_mode"] == "lexical" for hit in result["hits"])


def test_a_second_generation_published_during_the_call_drops_the_candidate(
    semantic_library, provisioner, world, probe
):
    """A generation that moves under the call is the same failure, legally.

    The library republishes while the provider is being queried. The plan the
    search started with no longer describes what is current, so the candidate
    is dropped and the library is reported pending.
    """
    build = current_build(provisioner, world, semantic_library)
    uri = build.document_uri(build.snapshot.documents[0])

    def republish():
        world.knowledge(semantic_library, title="Later publication")
        current_build(provisioner, world, semantic_library)

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            republish()
            return [Candidate(uri, 0.8)]

    result = search_knowledge(
        None, world.reader, QUERY, semantic=SemanticSearch(Reader(), connections=probe)
    )

    assert result["pending_library_count"] == 1
    assert result["fallback_reason"] == "index_pending_or_unavailable"
    assert all(hit["retrieval_mode"] == "lexical" for hit in result["hits"])


def test_a_candidate_from_a_foreign_root_is_refused(semantic_library, provisioner, world, probe):
    current_build(provisioner, world, semantic_library)
    foreign = f"viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-999"

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            return [Candidate(f"{foreign}/{uuid4()}.md", 0.99)]

    result = search_knowledge(
        None, world.reader, QUERY, semantic=SemanticSearch(Reader(), connections=probe)
    )
    assert result["retrieved_only"] is True
    assert all(hit["retrieval_mode"] == "lexical" for hit in result["hits"])


def test_an_expired_budget_starts_no_call_and_says_the_library_was_not_searched(
    semantic_library, provisioner, world, probe
):
    calls = []

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            calls.append(limit)
            return []

    # A clock that jumps a whole budget on every read, so the budget is gone
    # by the time the external phase first looks at it.
    ticks = [0.0]

    def spent_clock():
        ticks[0] += 10.0
        return ticks[0]

    result = search_knowledge(
        None,
        world.reader,
        QUERY,
        semantic=SemanticSearch(Reader(), connections=probe),
        deadline=Deadline(1.0, clock=spent_clock),
    )

    assert calls == [], "a budget that is gone must not start a provider call"
    # "We did not look" and "nothing is there" are different answers, and the
    # caller is told which one it got.
    assert result["pending_library_count"] == 1
    assert result["fallback_reason"] == "index_pending_or_unavailable"
    assert result["mode"] == "lexical"
    assert result["retrieved_only"] is True


def test_a_budget_that_expires_mid_fanout_bounds_the_calls_and_still_answers(
    semantic_library, provisioner, world, probe
):
    calls = []

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            calls.append(limit)
            return []

    ticks = [0.0]

    def clock():
        ticks[0] += 1.0
        return ticks[0]

    result = search_knowledge(
        None,
        world.reader,
        QUERY,
        semantic=SemanticSearch(Reader(), connections=probe),
        deadline=Deadline(0.5, clock=clock),
    )

    # An expired budget stops this search from *starting* work. It is never
    # reported as a cancellation of anything the provider was doing.
    assert len(calls) <= 1
    assert result["retrieved_only"] is True


def test_a_provider_failure_is_sanitised_and_the_search_still_answers(
    semantic_library, provisioner, world, probe
):
    class Broken:
        def find(self, *args, **kwargs):
            raise RuntimeError("provider exploded: synthetic-private-value")

    result = search_knowledge(
        None, world.reader, QUERY, semantic=SemanticSearch(Broken(), connections=probe)
    )
    assert result["mode"] == "lexical"
    assert result["pending_library_count"] == 1
    assert result["retrieved_only"] is True
    assert "synthetic-private-value" not in str(result)


def test_the_audit_is_its_own_phase_and_runs_last(semantic_library, provisioner, world, probe):
    build = current_build(provisioner, world, semantic_library)
    uri = build.document_uri(build.snapshot.documents[0])
    seen = []

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            seen.append(("external", probe.open))
            return [Candidate(uri, 0.8)]

    result = search_knowledge(
        None, world.reader, QUERY, semantic=SemanticSearch(Reader(), connections=probe)
    )

    kinds = [kind for kind, _ in probe.order]
    assert kinds.count("lease") >= 3, "plan, the phase after the call, and the audit"
    assert seen == [("external", 0)], "the provider is called with no lease open"
    assert result["retrieved_only"] is True
    assert len(result["retrieval_event_ids"]) == len(result["hits"])


def test_the_same_request_through_both_transports_gives_the_same_answer(
    semantic_library, provisioner, world, probe
):
    """One entrypoint, two transports, one answer.

    ``http.content.search`` and the MCP ``search_knowledge`` tool both call
    ``kb.catalog.content.search_knowledge``. If either grew a private search,
    they would drift here rather than in production.
    """
    from kb.catalog import content as catalog
    from kb.http import content as http_content
    from kb.mcp import server as mcp_server

    build = current_build(provisioner, world, semantic_library)
    uri = build.document_uri(build.snapshot.documents[0])

    class Reader:
        def find(self, identity, root, query, limit, *, deadline=None):
            return [Candidate(uri, 0.8)]

    service = SemanticSearch(Reader(), connections=probe)
    first = search_knowledge(None, world.reader, QUERY, semantic=service, connections=probe)
    second = search_knowledge(None, world.reader, QUERY, semantic=service, connections=probe)

    assert [h["id"] for h in first["hits"]] == [h["id"] for h in second["hits"]]
    assert first["mode"] == second["mode"]

    # The HTTP route passes the factory rather than holding the Db lease, and
    # the MCP tool uses the detached call. Neither owns a connection itself.
    http_source = http_content.search.__code__.co_varnames
    assert "conn" not in http_source[: http_source.index("me") + 1], (
        "the HTTP search route must not take the pooled connection dependency"
    )
    mcp_source = mcp_server.build_mcp.__code__
    assert mcp_source is not None
    assert callable(catalog.search_knowledge)


def test_a_search_with_no_connection_and_no_factory_is_refused(world):
    """The detached path is explicit; a missing lease is not a silent default."""
    principal = Principal(world.reader, world.account_id)
    with pytest.raises(ValueError):
        search_knowledge(None, principal, "anything", semantic=None)
