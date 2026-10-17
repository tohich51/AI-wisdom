"""Real PostgreSQL acceptance with synthetic concepts and a deterministic embedder."""

from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.research import semantic
from kb.research.similarity import (
    concept_text,
    concept_text_hash,
    enqueue_similarity,
    rebuild_similarity,
)
from kb.retrieval.embedding_profile import SELECTED_PROFILE

pytestmark = pytest.mark.integration


def _concept(conn, principal, library_id, label):
    return semantic.create_concept(
        conn, principal, library_id, label, f"Synthetic body for {label}."
    )["id"]


def _concepts(conn, principal, library_id, count):
    return [_concept(conn, principal, library_id, f"Concept {index}") for index in range(count)]


def _embedder(calls):
    def embed(texts):
        assert len(texts) <= 16
        calls.append(tuple(texts))
        result = []
        for offset, _text in enumerate(texts):
            vector = [0.0] * 4096
            vector[0] = 1.0
            vector[1 + offset % 16] = 0.02 * (offset + 1)
            result.append(vector)
        return result

    return embed


def _selection_embedder(calls):
    specifications = [
        ("A", (1.0, 0.0)),
        ("B", (0.8, 0.6)),
    ]
    specifications.extend((f"Midpoint {index}", (0.9, 0.3)) for index in range(6))
    vectors = {}
    for label, pair in specifications:
        vector = [0.0] * 4096
        vector[0], vector[1] = pair
        vectors[concept_text(label, f"Synthetic body for {label}.")] = vector

    def embed(texts):
        calls.append(tuple(texts))
        return [vectors[text] for text in texts]

    return embed


def _cache_counts(conn, principal, library_id):
    with transaction_identity(conn, principal):
        embeddings = conn.execute(
            "SELECT count(*) AS n FROM kb.concept_embedding WHERE library_id=%s",
            (library_id,),
        ).fetchone()[0]
        edges = conn.execute(
            "SELECT count(*) AS n FROM kb.concept_similarity WHERE library_id=%s",
            (library_id,),
        ).fetchone()[0]
    return embeddings, edges


def test_similarity_readiness_requires_exact_concept_hash_pairs(world):
    with psycopg.connect(world["dsn"]) as conn:
        ids = _concepts(conn, world["principal"], world["lib"], 2)
    hashes = [
        concept_text_hash(f"Concept {index}", f"Synthetic body for Concept {index}.")
        for index in range(2)
    ]
    vector = [1.0] + [0.0] * 4095

    # The first concept deliberately has only the second concept's hash.
    # Independent ANY predicates would incorrectly report both as ready.
    with psycopg.connect(world["worker"]) as conn:
        with transaction_identity(conn, world["principal"]):
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO kb.concept_embedding(
                         concept_id,library_id,profile_hash,text_hash,embedding
                       ) VALUES(%s,%s,%s,%s,%s)""",
                    [
                        (
                            ids[0],
                            world["lib"],
                            SELECTED_PROFILE.profile_hash,
                            hashes[1],
                            vector,
                        ),
                        (
                            ids[1],
                            world["lib"],
                            SELECTED_PROFILE.profile_hash,
                            hashes[1],
                            vector,
                        ),
                    ],
                )

    with psycopg.connect(world["dsn"]) as conn:
        similarity = semantic.semantic_network(conn, world["principal"], world["lib"])["similarity"]
    assert similarity["total"] == 2
    assert similarity["ready"] == 1
    assert similarity["available"] is False


def test_positive_rebuild_cache_hit_and_stale_revision_are_excluded(world):
    with psycopg.connect(world["dsn"]) as conn:
        ids = _concepts(conn, world["principal"], world["lib"], 5)
    calls = []
    first = rebuild_similarity(
        world["worker"], world["principal"], world["lib"], embedder=_embedder(calls)
    )
    assert first["ready"] == 5 and first["edges"]
    actual_texts = [text for batch in calls for text in batch]
    expected_texts = {
        concept_text(f"Concept {index}", f"Synthetic body for Concept {index}.")
        for index in range(5)
    }
    assert len(actual_texts) == 5
    assert set(actual_texts) == expected_texts
    assert calls and all(len(batch) <= 16 for batch in calls)

    rebuild_similarity(
        world["worker"],
        world["principal"],
        world["lib"],
        embedder=lambda _texts: pytest.fail("cached concepts reached the embedder"),
    )
    with psycopg.connect(world["dsn"]) as conn:
        graph = semantic.semantic_network(conn, world["principal"], world["lib"])
        assert graph["similarity"]["available"] is True
        assert graph["similarity"]["reason"] is None
        assert graph["similarity"]["profile_hash"] == SELECTED_PROFILE.profile_hash
        assert graph["similarity"]["ready"] == graph["similarity"]["total"] == 5
        assert graph["similarity"]["edges"]
        assert all(
            edge["symmetric"] is True and edge["score"] >= 0.65
            for edge in graph["similarity"]["edges"]
        )

        revised = semantic.create_concept(
            conn,
            world["principal"],
            world["lib"],
            "Revised concept",
            "A distinct synthetic body.",
            ids[0],
        )
        stale_filtered = semantic.semantic_network(conn, world["principal"], world["lib"])[
            "similarity"
        ]
        assert stale_filtered["ready"] == 5
        assert stale_filtered["total"] == 6
        assert revised["publication"] == "draft"
        revised_id = str(revised["id"])
        assert all(
            revised_id
            not in {
                edge["source"].split(":", 1)[1],
                edge["target"].split(":", 1)[1],
            }
            for edge in stale_filtered["edges"]
        )


def test_build_selection_supersedes_historical_top_three_edges(world):
    with psycopg.connect(world["dsn"]) as conn:
        concept_a = _concept(conn, world["principal"], world["lib"], "A")
        concept_b = _concept(conn, world["principal"], world["lib"], "B")

    calls = []
    first = rebuild_similarity(
        world["worker"], world["principal"], world["lib"], embedder=_selection_embedder(calls)
    )
    assert first["ready"] == 2 and first["edges"] == 1

    def current_similarity():
        with psycopg.connect(world["dsn"]) as conn:
            return semantic.semantic_network(conn, world["principal"], world["lib"])["similarity"]

    node_a = "concept:" + str(concept_a)
    node_b = "concept:" + str(concept_b)
    initial = current_similarity()
    assert initial["available"] is True
    assert any({edge["source"], edge["target"]} == {node_a, node_b} for edge in initial["edges"])

    cached = rebuild_similarity(
        world["worker"],
        world["principal"],
        world["lib"],
        embedder=lambda _texts: pytest.fail("unchanged capture reached the embedder"),
    )
    assert cached["ready"] == 2

    with psycopg.connect(world["dsn"]) as conn:
        midpoint_ids = [
            _concept(conn, world["principal"], world["lib"], f"Midpoint {index}")
            for index in range(6)
        ]

    stale = current_similarity()
    assert stale["available"] is False
    assert stale["ready"] == 2 and stale["total"] == 8
    assert stale["edges"] == []

    calls_before_rebuild = len(calls)
    rebuilt = rebuild_similarity(
        world["worker"],
        world["principal"],
        world["lib"],
        embedder=_selection_embedder(calls),
    )
    assert rebuilt["ready"] == 8 and rebuilt["edges"]
    assert len(calls) == calls_before_rebuild + 1
    assert set(calls[-1]) == {
        concept_text(f"Midpoint {index}", f"Synthetic body for Midpoint {index}.")
        for index in range(6)
    }

    final = current_similarity()
    assert final["available"] is True
    assert final["reason"] is None
    assert final["ready"] == final["total"] == 8
    assert all({edge["source"], edge["target"]} != {node_a, node_b} for edge in final["edges"])
    midpoint_nodes = {"concept:" + str(concept_id) for concept_id in midpoint_ids}
    for endpoint in (node_a, node_b):
        incident = [edge for edge in final["edges"] if endpoint in {edge["source"], edge["target"]}]
        assert len(incident) == 3
        for edge in incident:
            other = edge["target"] if edge["source"] == endpoint else edge["source"]
            assert other in midpoint_nodes

    ordered_pair = sorted((concept_a, concept_b), key=str)
    with psycopg.connect(world["worker"]) as conn:
        with transaction_identity(conn, world["principal"]):
            historical_pair_cached = conn.execute(
                """SELECT EXISTS(
                     SELECT 1 FROM kb.concept_similarity
                     WHERE library_id=%s AND profile_hash=%s
                       AND source_concept=%s AND target_concept=%s
                   )""",
                (
                    world["lib"],
                    SELECTED_PROFILE.profile_hash,
                    ordered_pair[0],
                    ordered_pair[1],
                ),
            ).fetchone()[0]
    assert historical_pair_cached is True


def test_revocation_cloud_foreign_and_draft_acl_never_publish_derived_rows(world):
    with psycopg.connect(world["dsn"]) as conn:
        _concepts(conn, world["principal"], world["lib"], 3)

    def revoke_during_provider(texts):
        with psycopg.connect(world["admin"]) as admin:
            admin.execute(
                """INSERT INTO kb.library_policy(library_id,external_processing_allowed)
                  VALUES(%s,false)
                  ON CONFLICT(library_id) DO UPDATE SET external_processing_allowed=false""",
                (world["lib"],),
            )
        return _embedder([])(texts)

    with pytest.raises(AccessDenied):
        rebuild_similarity(
            world["worker"],
            world["principal"],
            world["lib"],
            embedder=revoke_during_provider,
        )
    with psycopg.connect(world["dsn"]) as conn:
        assert _cache_counts(conn, world["principal"], world["lib"]) == (0, 0)

    with psycopg.connect(world["admin"]) as admin:
        admin.execute(
            """INSERT INTO kb.library_policy(library_id,external_processing_allowed)
              VALUES(%s,true)
              ON CONFLICT(library_id) DO UPDATE SET external_processing_allowed=true""",
            (world["lib"],),
        )
    cloud = replace(world["principal"], cloud_client=True)
    foreign = Principal(uuid4(), uuid4(), cloud_client=False)
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied):
            enqueue_similarity(conn, cloud, world["lib"])
        with pytest.raises(AccessDenied):
            enqueue_similarity(conn, foreign, world["lib"])

    rebuild_similarity(world["worker"], world["principal"], world["lib"], embedder=_embedder([]))
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            conn.execute(
                """INSERT INTO kb.library_grant(library_id,principal_id,role)
                  VALUES(%s,%s,'reader') ON CONFLICT DO NOTHING""",
                (world["lib"], world["other"].principal_id),
            )
        other_graph = semantic.semantic_network(conn, world["other"], world["lib"])
        assert other_graph["nodes"] == []
        assert other_graph["similarity"]["total"] == 0
        assert other_graph["similarity"]["edges"] == []
        with transaction_identity(conn, world["principal"]):
            owner_builds = conn.execute(
                """SELECT count(*) FROM kb.concept_similarity_build
                   WHERE library_id=%s""",
                (world["lib"],),
            ).fetchone()[0]
        with transaction_identity(conn, world["other"]):
            reader_builds = conn.execute(
                """SELECT count(*) FROM kb.concept_similarity_build
                   WHERE library_id=%s""",
                (world["lib"],),
            ).fetchone()[0]
        assert owner_builds == 1
        assert reader_builds == 0


def test_provider_failure_and_malformed_later_batch_are_atomic(world):
    with psycopg.connect(world["dsn"]) as conn:
        _concepts(conn, world["principal"], world["lib"], 17)

    def malformed_later_batch(texts):
        if len(texts) == 16:
            return _embedder([])(texts)
        return [[1.0]]

    with pytest.raises(ValueError, match="malformed_embedding_dimension"):
        rebuild_similarity(
            world["worker"],
            world["principal"],
            world["lib"],
            embedder=malformed_later_batch,
        )
    with psycopg.connect(world["dsn"]) as conn:
        assert _cache_counts(conn, world["principal"], world["lib"]) == (0, 0)

    calls = []

    def fail_later_batch(texts):
        calls.append(tuple(texts))
        if len(calls) == 1:
            return _embedder([])(texts)
        raise RuntimeError("synthetic provider failure")

    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        rebuild_similarity(
            world["worker"], world["principal"], world["lib"], embedder=fail_later_batch
        )
    assert len(calls) == 2
    with psycopg.connect(world["dsn"]) as conn:
        assert _cache_counts(conn, world["principal"], world["lib"]) == (0, 0)


def test_sql_embedding_vector_validation_rejects_malformed_shapes(world):
    valid_expression = (
        "ARRAY[1.0::double precision] || array_fill(0.0::double precision,ARRAY[4095])"
    )
    invalid_expressions = (
        "NULL::double precision[]",
        "array_fill(0.0::double precision,ARRAY[4096])",
        ("ARRAY[NULL::double precision] || array_fill(0.0::double precision,ARRAY[4095])"),
        ("ARRAY['NaN'::double precision] || array_fill(0.0::double precision,ARRAY[4095])"),
        ("ARRAY['Infinity'::double precision] || array_fill(0.0::double precision,ARRAY[4095])"),
        "array_fill(1.0::double precision,ARRAY[2048,2])",
    )
    with psycopg.connect(world["worker"]) as conn:
        with transaction_identity(conn, world["principal"]):
            valid = conn.execute(
                f"SELECT kb.valid_concept_embedding({valid_expression},4096)"
            ).fetchone()[0]
            assert valid is True
            for expression in invalid_expressions:
                result = conn.execute(
                    f"SELECT kb.valid_concept_embedding({expression},4096)"
                ).fetchone()[0]
                assert result is False


def test_queue_deduplicates_for_actor_and_library(world):
    with psycopg.connect(world["dsn"]) as conn:
        assert enqueue_similarity(conn, world["principal"], world["lib"]) == {
            "status": "no_concepts",
            "library_id": world["lib"],
        }
        _concepts(conn, world["principal"], world["lib"], 2)
        first = enqueue_similarity(conn, world["principal"], world["lib"])
        second = enqueue_similarity(conn, world["principal"], world["lib"])
    assert first == {"status": "queued", "library_id": world["lib"]}
    assert second == {"status": "already_queued", "library_id": world["lib"]}
