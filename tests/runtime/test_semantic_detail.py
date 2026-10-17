"""Real PostgreSQL boundaries for bounded, freshly validated concept details."""

from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_runtime import runtime_db as runtime_db
from test_runtime import source as source
from test_runtime import world as world

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import libraries
from kb.jobs.ingestion import run_job, submit_processing
from kb.research import grounding, semantic, similarity
from kb.research.grounding import capture_snapshot, resolve_citation

pytestmark = pytest.mark.integration


def _parsed(world, text):
    ident = source(world, text)
    with psycopg.connect(world["dsn"]) as conn:
        job = submit_processing(conn, world["principal"], ident, str(uuid4()))
    run_job(world["worker"], job["job_id"], world["principal"], world["store"].root)
    return ident


def _grounded_concept(world, text, label):
    source_id = _parsed(world, text)
    with psycopg.connect(world["dsn"]) as conn:
        snapshot = capture_snapshot(conn, world["principal"], [source_id])
        fragment = snapshot["fragments"][0]
        with (
            transaction_identity(conn, world["principal"]),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            citation = resolve_citation(cur, snapshot["manifest"], UUID(fragment["id"]), text)
            concept_id = uuid4()
            cur.execute(
                """INSERT INTO kb.semantic_concept(
                     id,library_id,principal_id,lineage_id,version_no,
                     label,body,origin,manifest,citations)
                   VALUES(%s,%s,%s,%s,1,%s,%s,'manual_revision',%s,%s)""",
                (
                    concept_id,
                    world["lib"],
                    world["principal"].principal_id,
                    concept_id,
                    label,
                    text,
                    Jsonb(snapshot["manifest"]),
                    Jsonb([citation]),
                ),
            )
    return concept_id


def _set_reader_grant(world, enabled):
    with psycopg.connect(world["admin"]) as admin:
        if enabled:
            admin.execute(
                "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,'reader')",
                (world["lib"], world["other"].principal_id),
            )
        else:
            admin.execute(
                "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
                (world["lib"], world["other"].principal_id),
            )


def test_get_concept_matches_graph_serializer_without_similarity(world, monkeypatch):
    concept_id = _grounded_concept(
        world,
        "A bounded detail read still validates exact source evidence.",
        "Bounded detail",
    )
    similarity_calls = []

    def fake_similarity(*args):
        similarity_calls.append(args)
        return {
            "available": False,
            "ready": 1,
            "total": 1,
            "edges": [],
            "truncated": False,
            "reason": "test",
        }

    monkeypatch.setattr(similarity, "similarity_layer", fake_similarity)
    with psycopg.connect(world["dsn"]) as conn:
        detail = semantic.get_concept(conn, world["principal"], world["lib"], concept_id)
        assert similarity_calls == []
        graph = semantic.semantic_network(conn, world["principal"], world["lib"], limit=200)

    assert len(similarity_calls) == 1
    graph_node = next(node for node in graph["nodes"] if node["entity_id"] == concept_id)
    assert detail == graph_node
    assert detail["evidence"]


def test_get_concept_is_independent_of_graph_limit(monkeypatch, world):
    def forbidden_similarity(*args, **kwargs):
        pytest.fail("concept detail queried the similarity layer")

    monkeypatch.setattr(similarity, "similarity_layer", forbidden_similarity)
    concept_ids = sorted(uuid4() for _ in range(201))
    target = concept_ids[-1]

    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO kb.semantic_concept(
                         id,library_id,principal_id,lineage_id,version_no,
                         label,body,origin,citations,created_at)
                       SELECT item_id,%s,%s,item_id,1,
                              'Concept ' || ordinality::text,
                              'Body ' || ordinality::text,
                              'manual',%s,
                              CURRENT_TIMESTAMP
                                + (ordinality::double precision * INTERVAL '1 second')
                       FROM unnest(%s::uuid[]) WITH ORDINALITY
                            AS generated(item_id,ordinality)""",
                    (
                        world["lib"],
                        world["principal"].principal_id,
                        Jsonb([]),
                        concept_ids,
                    ),
                )
            visible = {
                row[0]
                for row in conn.execute(
                    "SELECT id FROM kb.semantic_concept WHERE library_id=%s "
                    "AND publication<>'withdrawn' ORDER BY created_at,id LIMIT 200",
                    (world["lib"],),
                ).fetchall()
            }
            detail = semantic.get_concept(conn, world["principal"], world["lib"], target)

    assert target not in visible
    assert detail["entity_id"] == target


def test_get_concept_keeps_other_author_draft_private_until_published(world):
    concept_id = _grounded_concept(
        world,
        "A reader grant does not disclose another author's private draft.",
        "Private grounded draft",
    )
    _set_reader_grant(world, True)

    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied, match="forbidden"):
            semantic.get_concept(conn, world["other"], world["lib"], concept_id)

        semantic.review_semantic(
            conn,
            world["principal"],
            concept_id,
            "concept",
            "publish",
        )
        published = semantic.get_concept(conn, world["other"], world["lib"], concept_id)

    assert published["entity_id"] == concept_id
    assert published["evidence"]


def test_get_concept_rejects_foreign_library_and_unknown_id(world):
    with psycopg.connect(world["dsn"]) as conn:
        other_library = libraries.create_library(
            conn,
            world["principal"],
            libraries.CreateLibrary(
                organisation_id=world["principal"].account_id,
                name=str(uuid4()),
                kind="reference",
            ),
        )
        foreign = semantic.create_concept(
            conn,
            world["principal"],
            other_library.id,
            "Foreign-library concept",
            "The supplied library must own the concept.",
        )
        semantic.review_semantic(
            conn,
            world["principal"],
            foreign["id"],
            "concept",
            "publish",
        )

        with pytest.raises(AccessDenied, match="forbidden"):
            semantic.get_concept(conn, world["principal"], world["lib"], foreign["id"])
        with pytest.raises(AccessDenied, match="forbidden"):
            semantic.get_concept(conn, world["principal"], world["lib"], uuid4())


def test_published_concept_rereads_evidence_and_honors_access_boundaries(world, monkeypatch):
    concept_id = _grounded_concept(
        world,
        "Published concept details must freshly validate their canonical evidence.",
        "Published canonical detail",
    )
    with psycopg.connect(world["dsn"]) as conn:
        semantic.review_semantic(
            conn,
            world["principal"],
            concept_id,
            "concept",
            "publish",
        )
    _set_reader_grant(world, True)

    snapshot_checks = []
    citation_checks = []
    real_check_snapshot = grounding.check_snapshot
    real_validate_citation = semantic.validate_citations

    def tracked_check_snapshot(*args, **kwargs):
        snapshot_checks.append(True)
        return real_check_snapshot(*args, **kwargs)

    def tracked_validate_citation(*args, **kwargs):
        citation_checks.append(True)
        return real_validate_citation(*args, **kwargs)

    monkeypatch.setattr(grounding, "check_snapshot", tracked_check_snapshot)
    monkeypatch.setattr(semantic, "validate_citations", tracked_validate_citation)

    with psycopg.connect(world["dsn"]) as conn:
        first = semantic.get_concept(conn, world["other"], world["lib"], concept_id)
        first_snapshot_count = len(snapshot_checks)
        first_citation_count = len(citation_checks)
        second = semantic.get_concept(conn, world["other"], world["lib"], concept_id)

    assert first["entity_id"] == second["entity_id"] == concept_id
    assert first["evidence"] and second["evidence"]
    assert first_snapshot_count > 0
    assert first_citation_count > 0
    assert len(snapshot_checks) > first_snapshot_count
    assert len(citation_checks) > first_citation_count

    _set_reader_grant(world, False)
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied, match="forbidden"):
            semantic.get_concept(conn, world["other"], world["lib"], concept_id)

    _set_reader_grant(world, True)
    with psycopg.connect(world["dsn"]) as conn:
        semantic.review_semantic(
            conn,
            world["principal"],
            concept_id,
            "concept",
            "withdraw",
        )

    for principal in (world["principal"], world["other"]):
        with psycopg.connect(world["dsn"]) as conn:
            with pytest.raises(AccessDenied, match="forbidden"):
                semantic.get_concept(conn, principal, world["lib"], concept_id)
