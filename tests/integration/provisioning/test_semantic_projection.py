"""Real PostgreSQL projection/CAS/ACL checks; SDK double tests no provider claim."""

from uuid import uuid4

import psycopg
import pytest

from kb.access.policy import transaction_identity
from kb.adapters.vector_clients import Deadline
from kb.adapters.vector_reader import Candidate, IndexUnavailable
from kb.catalog.content import search_knowledge
from kb.retrieval import semantic_registry as registry
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.semantic_search import SemanticSearch

pytestmark = pytest.mark.integration


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
    world.knowledge(lib, title="Unique canonical marketing statement")
    yield lib
    # Published knowledge and its provenance are intentionally immutable.
    # The session-scoped ephemeral PostgreSQL destroys this whole catalogue.


def current(provisioner, world, lib):
    build = registry.begin(
        provisioner, world.manager, registry.capture(provisioner, world.manager, lib)
    )
    if not build.already_current:
        for doc in build.snapshot.documents:
            registry.checkpoint(provisioner, world.manager, build, doc)
        registry.activate(provisioner, world.manager, build)
    return build


class ReaderDouble:
    def __init__(self, build, action=lambda: None, items=None):
        self.build, self.action, self.items, self.calls = build, action, items, 0
        self.deadlines = []

    def find(self, identity, root, query, limit, *, deadline=None):
        # ``deadline`` is part of the reader contract now: the search hands the
        # shared budget to the call instead of the call choosing its own.
        self.calls += 1
        self.deadlines.append(deadline)
        self.action()
        return (
            self.items
            if self.items is not None
            else [Candidate(self.build.document_uri(self.build.snapshot.documents[0]), 0.8)]
        )


@pytest.mark.parametrize("semantic_enabled", [False, True])
def test_source_filter_precedes_ranking_and_limit_and_cannot_reveal_unknown_source(
    semantic_library, provisioner, world, admin, gateway, semantic_enabled
):
    sources = [uuid4(), uuid4()]
    ids = []
    for source_id in sources:
        admin.execute(
            "INSERT INTO kb.source(id,library_id,title,media_type,submitted_by,"
            "object_key,content_hash) "
            "VALUES(%s,%s,'Synthetic filter','text/plain',%s,%s,%s)",
            (source_id, semantic_library, world.owner, source_id.hex, "b" * 64),
        )
        ident = world.knowledge(semantic_library, title="Unique filter needle", publication="draft")
        admin.execute(
            "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id) VALUES(%s,%s)",
            (ident, source_id),
        )
        admin.execute("UPDATE kb.knowledge SET publication='published' WHERE id=%s", (ident,))
        ids.append(ident)
    service = None
    if semantic_enabled:
        build = current(provisioner, world, semantic_library)
        docs = {d.id: d for d in build.snapshot.documents}
        reader = ReaderDouble(
            build,
            items=[
                Candidate(build.document_uri(docs[i]), 0.9 - n * 0.1) for n, i in enumerate(ids)
            ],
        )
        service = SemanticSearch(reader)
    result = search_knowledge(
        gateway,
        world.reader,
        "Unique filter needle",
        [semantic_library],
        1,
        semantic=service,
        source_ids=[sources[1]],
    )
    assert [h["id"] for h in result["hits"]] == [ids[1]]
    for source_scope in [[], [uuid4()]]:
        assert (
            search_knowledge(
                gateway,
                world.reader,
                "Unique filter needle",
                [semantic_library],
                1,
                semantic=service,
                source_ids=source_scope,
            )["hits"]
            == []
        )


def test_semantic_hydrates_canonical_text_and_generation_is_idempotent(
    semantic_library, provisioner, world, gateway
):
    build = current(provisioner, world, semantic_library)
    same = registry.begin(provisioner, world.manager, build.snapshot)
    assert same.already_current and same.generation == build.generation
    reader = ReaderDouble(build)
    result = search_knowledge(
        gateway, world.reader, "unrelated paraphrase", semantic=SemanticSearch(reader)
    )
    assert result["mode"] == "semantic" and result["pending_library_count"] == 0
    assert result["hits"][0]["statement"] == build.snapshot.documents[0].statement
    assert result["hits"][0]["retrieval_mode"] == "semantic"
    assert "resource_uri" not in result["hits"][0]
    assert reader.calls == 1
    # The reader is handed the one shared budget rather than picking its own.
    assert len(reader.deadlines) == 1
    assert isinstance(reader.deadlines[0], Deadline)
    assert not reader.deadlines[0].expired


@pytest.mark.parametrize("operation", ["hash_null", "verified_null", "map_delete", "map_update"])
def test_current_manifest_cannot_be_rewritten(semantic_library, provisioner, world, operation):
    build = current(provisioner, world, semantic_library)
    statements = {
        "hash_null": "UPDATE kb.index_generation SET content_hash=NULL WHERE library_id=%s",
        "verified_null": "UPDATE kb.index_generation SET verified_at=NULL WHERE library_id=%s",
        "map_delete": "DELETE FROM kb.index_document WHERE library_id=%s",
        "map_update": "UPDATE kb.index_document SET statement_hash=repeat('a',64) "
        "WHERE library_id=%s",
    }
    with pytest.raises(psycopg.Error), transaction_identity(provisioner, world.manager):
        provisioner.execute(statements[operation], (build.snapshot.library_id,))


@pytest.mark.parametrize("failure", ["partial", "revision", "profile"])
def test_activation_rejects_partial_stale_or_mixed_profile(
    semantic_library, provisioner, world, admin, failure
):
    build = registry.begin(
        provisioner, world.manager, registry.capture(provisioner, world.manager, semantic_library)
    )
    if failure != "partial":
        for doc in build.snapshot.documents:
            registry.checkpoint(provisioner, world.manager, build, doc)
    if failure == "revision":
        world.knowledge(semantic_library, title="Later publication")
    if failure == "profile":
        admin.execute(
            "UPDATE kb.index_account SET state='retired' WHERE library_id=%s", (semantic_library,)
        )
    with pytest.raises(psycopg.Error):
        registry.activate(provisioner, world.manager, build)
    with transaction_identity(provisioner, world.manager):
        assert (
            provisioner.execute(
                "SELECT state FROM kb.index_generation WHERE library_id=%s", (semantic_library,)
            ).fetchone()[0]
            == "building"
        )


def test_revoked_during_vector_call_returns_no_content_or_scope(
    semantic_library, provisioner, world, admin, gateway
):
    build = current(provisioner, world, semantic_library)
    reader = ReaderDouble(
        build,
        lambda: admin.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (semantic_library, world.colleague),
        ),
    )
    result = search_knowledge(gateway, world.reader, "paraphrase", semantic=SemanticSearch(reader))
    assert result["hits"] == [] and result["indexed_library_count"] == 0
    assert result["pending_library_count"] == 0


def test_generation_changed_during_vector_call_drops_old_candidates(
    semantic_library, provisioner, world, gateway
):
    build = current(provisioner, world, semantic_library)

    def swap():
        world.knowledge(semantic_library, title="Later publication")
        current(provisioner, world, semantic_library)

    result = search_knowledge(
        gateway, world.reader, "paraphrase", semantic=SemanticSearch(ReaderDouble(build, swap))
    )
    assert result["hits"] == [] and result["pending_library_count"] == 1
    assert result["mode"] == "lexical"


def test_forged_uri_is_not_hydrated(semantic_library, provisioner, world, gateway):
    build = current(provisioner, world, semantic_library)
    reader = ReaderDouble(build, items=[Candidate(f"{build.resource_root}/{uuid4()}.md", 0.99)])
    assert (
        search_knowledge(gateway, world.reader, "paraphrase", semantic=SemanticSearch(reader))[
            "hits"
        ]
        == []
    )


def test_pending_index_uses_explicit_lexical_fallback(semantic_library, world, gateway):
    class NeverReader:
        def find(self, *args, **kwargs):
            raise AssertionError("No active index should be queried")

    result = search_knowledge(
        gateway, world.reader, "Unique", semantic=SemanticSearch(NeverReader())
    )
    assert result["mode"] == "lexical" and result["pending_library_count"] == 1
    assert result["hits"][0]["retrieval_mode"] == "lexical"


def test_backend_failure_falls_back_without_raw_error(
    semantic_library, provisioner, world, gateway
):
    build = current(provisioner, world, semantic_library)

    def fail():
        raise IndexUnavailable("synthetic provider failure")

    result = search_knowledge(
        gateway, world.reader, "Unique", semantic=SemanticSearch(ReaderDouble(build, fail))
    )
    assert result["mode"] == "lexical" and result["hits"]
    assert "synthetic provider failure" not in str(result)


def test_gateway_and_worker_cannot_write_projection(semantic_library, gateway, world, c15_dsn):
    for conn in (gateway, psycopg.connect(c15_dsn, autocommit=True, options="-c role=kb_worker")):
        try:
            with pytest.raises(psycopg.Error), transaction_identity(conn, world.manager):
                conn.execute(
                    "INSERT INTO kb.index_document VALUES(%s,1,%s,%s,%s,now())",
                    (semantic_library, uuid4(), "viking://resources/index/a", "a" * 64),
                )
        finally:
            if conn is not gateway:
                conn.close()


def test_membership_removed_during_find_is_refused(
    semantic_library, provisioner, world, admin, gateway
):
    build = current(provisioner, world, semantic_library)
    reader = ReaderDouble(
        build,
        lambda: admin.execute(
            "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
            (world.account_id, world.colleague),
        ),
    )
    result = search_knowledge(gateway, world.reader, "paraphrase", semantic=SemanticSearch(reader))
    assert result["hits"] == [] and result["indexed_library_count"] == 0


def test_published_knowledge_cannot_move_its_source_library(semantic_library, world, admin):
    source = uuid4()
    admin.execute(
        "INSERT INTO kb.source(id,library_id,title,media_type,submitted_by,"
        "object_key,content_hash) "
        "VALUES(%s,%s,'Synthetic source','text/plain',%s,%s,%s)",
        (source, semantic_library, world.owner, source.hex, "a" * 64),
    )
    knowledge = world.knowledge(semantic_library, title="Draft", publication="draft")
    admin.execute(
        "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id) VALUES(%s,%s)",
        (knowledge, source),
    )
    other = world.library()
    with pytest.raises(psycopg.Error):
        admin.execute("UPDATE kb.knowledge SET library_id=%s WHERE id=%s", (other, knowledge))
    # Remove the synthetic draft provenance before parent cleanup.
    admin.execute("DELETE FROM kb.knowledge_provenance WHERE knowledge_id=%s", (knowledge,))
    admin.execute("DELETE FROM kb.library WHERE id=%s", (other,))


def test_withdrawn_source_invalidates_derived_vectors(
    semantic_library, provisioner, world, admin, gateway
):
    source = uuid4()
    admin.execute(
        "INSERT INTO kb.source(id,library_id,title,media_type,submitted_by,"
        "object_key,content_hash) "
        "VALUES(%s,%s,'Synthetic source','text/plain',%s,%s,%s)",
        (source, semantic_library, world.owner, source.hex, "a" * 64),
    )
    knowledge = world.knowledge(semantic_library, title="Derived synthetic", publication="draft")
    admin.execute(
        "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id) VALUES(%s,%s)",
        (knowledge, source),
    )
    admin.execute("UPDATE kb.knowledge SET publication='published' WHERE id=%s", (knowledge,))
    current(provisioner, world, semantic_library)
    admin.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (source,))
    snapshot = registry.capture(provisioner, world.manager, semantic_library)
    assert knowledge not in {d.id for d in snapshot.documents}
    result = search_knowledge(
        gateway, world.reader, "Derived", semantic=SemanticSearch(ReaderDouble(None))
    )
    assert result["hits"] == [] and result["mode"] == "lexical"
    current(provisioner, world, semantic_library)
    # Immutable published provenance is removed with the ephemeral test DB.


def test_revoked_writer_cannot_activate_but_can_cancel_own_build(
    semantic_library, provisioner, world, admin
):
    build = registry.begin(
        provisioner, world.manager, registry.capture(provisioner, world.manager, semantic_library)
    )
    for document in build.snapshot.documents:
        registry.checkpoint(provisioner, world.manager, build, document)
    admin.execute(
        "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
        (world.account_id, world.owner),
    )
    with pytest.raises((psycopg.Error, ValueError)):
        registry.activate(provisioner, world.manager, build)
    registry.fail(provisioner, world.manager, build)
    assert (
        admin.execute(
            "SELECT state FROM kb.index_generation WHERE library_id=%s", (semantic_library,)
        ).fetchone()[0]
        == "retired"
    )
