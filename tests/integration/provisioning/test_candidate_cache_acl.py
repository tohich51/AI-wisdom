"""Warm candidate cache must still obey live PostgreSQL permissions and provenance."""

from uuid import uuid4

import pytest
from test_semantic_projection import (
    ReaderDouble,
    current,
)
from test_semantic_projection import (
    semantic_library as semantic_library,
)

from kb.access.policy import Principal
from kb.catalog.content import search_knowledge
from kb.retrieval.candidate_cache import CandidateCache
from kb.retrieval.semantic_search import SemanticSearch

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("change", ["grant", "membership", "generation"])
def test_warm_candidates_do_not_survive_current_authority_change(
    semantic_library, provisioner, world, admin, gateway, change
):
    build = current(provisioner, world, semantic_library)
    reader = ReaderDouble(build)
    service = SemanticSearch(reader, candidates=CandidateCache())
    args = (gateway, world.reader, "unrelated paraphrase")

    first = search_knowledge(*args, semantic=service)
    second = search_knowledge(*args, semantic=service)
    assert first["hits"] and second["hits"] and reader.calls == 1
    if change == "grant":
        admin.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (semantic_library, world.colleague),
        )
    elif change == "membership":
        admin.execute(
            "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
            (world.account_id, world.colleague),
        )
    else:
        world.knowledge(semantic_library, title="Later publication")
    result = search_knowledge(*args, semantic=service)
    assert result["hits"] == [] and reader.calls == 1


def test_cached_candidates_remain_identity_filter_and_provenance_scoped(
    semantic_library, provisioner, world, admin, gateway
):
    build = current(provisioner, world, semantic_library)
    reader = ReaderDouble(build)
    service = SemanticSearch(reader, candidates=CandidateCache())
    query = "unrelated paraphrase"
    assert search_knowledge(gateway, world.reader, query, semantic=service)["hits"]
    assert search_knowledge(gateway, world.manager, query, semantic=service)["hits"]
    assert reader.calls == 2  # Same plans/query never share across principals.
    changed_watermark = Principal(
        world.reader.principal_id,
        world.reader.account_id,
        world.reader.generation_watermark + 1,
        world.reader.cloud_client,
    )
    assert search_knowledge(gateway, changed_watermark, query, semantic=service)["hits"]
    assert reader.calls == 3
    missing_source = uuid4()
    assert (
        search_knowledge(
            gateway, world.reader, query, semantic=service, source_ids=[missing_source]
        )["hits"]
        == []
    )
    assert reader.calls == 4
    # A source withdrawn after warming must suppress the same cached URI.
    source = uuid4()
    admin.execute(
        "INSERT INTO kb.source(id,library_id,title,media_type,submitted_by,"
        "object_key,content_hash) "
        "VALUES(%s,%s,'Synthetic cache source','text/plain',%s,%s,%s)",
        (source, semantic_library, world.owner, source.hex, "a" * 64),
    )
    # Published evidence is immutable: use a new draft with real provenance.
    ident = world.knowledge(semantic_library, title="Derived cache canary", publication="draft")
    admin.execute(
        "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id) VALUES(%s,%s)",
        (ident, source),
    )
    admin.execute("UPDATE kb.knowledge SET publication='published' WHERE id=%s", (ident,))
    new_build = current(provisioner, world, semantic_library)
    from kb.adapters.vector_reader import Candidate

    reader.items = [
        Candidate(new_build.document_uri(d), 0.8)
        for d in new_build.snapshot.documents
        if d.id == ident
    ]
    before = search_knowledge(gateway, world.reader, "different paraphrase", semantic=service)
    assert any(str(hit["id"]) == str(ident) for hit in before["hits"])
    calls = reader.calls
    admin.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (source,))
    after = search_knowledge(gateway, world.reader, "different paraphrase", semantic=service)
    assert all(str(hit["id"]) != str(ident) for hit in after["hits"])
    assert reader.calls == calls
