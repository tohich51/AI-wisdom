"""Real PostgreSQL regressions for graph-wide evidence validation."""

import hashlib
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


class _CountingCursor:
    def __init__(self, cursor, events=None):
        self._cursor = cursor
        self.statements = []
        self._events = events

    def execute(self, statement, *args, **kwargs):
        self.statements.append(statement)
        if self._events is not None:
            self._events.append("execute")
        return self._cursor.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


def _parsed(world_data, text):
    ident = source(world_data, text)
    with psycopg.connect(world_data["dsn"]) as conn:
        job = submit_processing(conn, world_data["principal"], ident, str(uuid4()))
    run_job(world_data["worker"], job["job_id"], world_data["principal"], world_data["store"].root)
    return ident


def _capture(world_data, source_ids, needle=None):
    with psycopg.connect(world_data["dsn"]) as conn:
        if needle is None:
            return capture_snapshot(conn, world_data["principal"], source_ids)
        return capture_snapshot(conn, world_data["principal"], source_ids, needle)


def _citation(world_data, snapshot, source_id, text, quote):
    fragment = next(row for row in snapshot["fragments"] if str(row["source_id"]) == str(source_id))
    with psycopg.connect(world_data["dsn"]) as conn:
        with (
            transaction_identity(conn, world_data["principal"]),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            return resolve_citation(
                cur,
                snapshot["manifest"],
                UUID(fragment["id"]),
                quote,
            )


def _new_reference_library(world_data):
    with psycopg.connect(world_data["dsn"]) as conn:
        return libraries.create_library(
            conn,
            world_data["principal"],
            libraries.CreateLibrary(
                organisation_id=world_data["principal"].account_id,
                name=f"graph-dependency-{uuid4()}",
                kind="reference",
            ),
        )


def _two_pin_material(world_data):
    dependency = _new_reference_library(world_data)
    first_text = "Independent first source: record the baseline."
    second_text = "Independent second source: preserve the dependency."
    first_id = _parsed(world_data, first_text)
    second_id = _parsed({**world_data, "lib": dependency.id}, second_text)
    snapshot = _capture(world_data, [first_id, second_id])
    citations = [
        _citation(world_data, snapshot, first_id, first_text, first_text),
        _citation(world_data, snapshot, second_id, second_text, second_text),
    ]
    return dependency.id, snapshot, citations


def _insert_concept(world_data, label, manifest, citations, publish=False):
    concept_id = uuid4()
    with psycopg.connect(world_data["dsn"]) as conn:
        with transaction_identity(conn, world_data["principal"]):
            conn.execute(
                """INSERT INTO kb.semantic_concept(
                     id,library_id,principal_id,lineage_id,version_no,
                     label,body,origin,manifest,citations)
                   VALUES(%s,%s,%s,%s,1,%s,%s,'manual_revision',%s,%s)""",
                (
                    concept_id,
                    world_data["lib"],
                    world_data["principal"].principal_id,
                    concept_id,
                    label,
                    "A synthetic summary backed by pinned source evidence.",
                    Jsonb(manifest),
                    Jsonb(citations),
                ),
            )
    if publish:
        with psycopg.connect(world_data["dsn"]) as conn:
            semantic.review_semantic(
                conn,
                world_data["principal"],
                concept_id,
                "concept",
                "publish",
            )
    return concept_id


def _grant_reader(world_data, library_id, principal):
    with psycopg.connect(world_data["admin"]) as admin:
        admin.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,'reader')",
            (library_id, principal.principal_id),
        )


def _revoke_reader(world_data, library_id, principal):
    with psycopg.connect(world_data["admin"]) as admin:
        deleted = admin.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (library_id, principal.principal_id),
        ).rowcount
    assert deleted == 1


def _manifest_readable(world_data, principal, manifest):
    with psycopg.connect(world_data["dsn"]) as conn:
        with transaction_identity(conn, principal):
            return conn.execute(
                "SELECT kb.can_read_manifest(%s)",
                (Jsonb(manifest),),
            ).fetchone() == (True,)


def _concept_visible(world_data, principal, library_id, concept_id):
    with psycopg.connect(world_data["dsn"]) as conn:
        with transaction_identity(conn, principal):
            return (
                conn.execute(
                    "SELECT 1 FROM kb.semantic_concept WHERE id=%s AND library_id=%s",
                    (concept_id, library_id),
                ).fetchone()
                is not None
            )


def _unavailable_similarity(*args, **kwargs):
    return {
        "available": False,
        "ready": 1,
        "total": 1,
        "edges": [],
        "truncated": False,
        "reason": "test",
    }


def test_graph_and_detail_preserve_repeated_canonical_evidence(world, monkeypatch):
    prefix = "Маркетинг 🎯: выберите сегмент. "
    quote = "Измерьте результат эксперимента."
    text = prefix + quote
    source_id = _parsed(world, text)
    snapshot = _capture(world, [source_id], quote)
    citation = _citation(world, snapshot, source_id, text, quote)

    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            with conn.cursor(row_factory=dict_row) as cur:
                canonical = grounding.validate_citation(cur, snapshot["manifest"], citation)

    first_id = _insert_concept(
        world,
        "Repeated evidence one",
        snapshot["manifest"],
        [citation, citation],
    )
    second_id = _insert_concept(
        world,
        "Repeated evidence two",
        snapshot["manifest"],
        [citation, citation],
    )
    similarity_calls = []

    def tracked_similarity(*args, **kwargs):
        similarity_calls.append(True)
        return _unavailable_similarity()

    monkeypatch.setattr(similarity, "similarity_layer", tracked_similarity)
    with psycopg.connect(world["dsn"]) as conn:
        first = semantic.get_concept(conn, world["principal"], world["lib"], first_id)
        second = semantic.get_concept(conn, world["principal"], world["lib"], second_id)
        assert similarity_calls == []
        graph = semantic.semantic_network(
            conn,
            world["principal"],
            world["lib"],
            limit=200,
        )

    nodes = {node["entity_id"]: node for node in graph["nodes"]}
    expected_evidence = [canonical, canonical]
    assert first == nodes[first_id]
    assert second == nodes[second_id]
    assert first["evidence"] == expected_evidence
    assert second["evidence"] == expected_evidence
    assert canonical["start"] == len(prefix)
    assert canonical["quote"] == text[canonical["start"] : canonical["end"]]
    assert canonical["text_hash"] == hashlib.sha256(text.encode()).hexdigest()
    assert canonical["locator"]["file_page"] is None
    assert all(item["locator"] == canonical["locator"] for item in first["evidence"])
    assert len(similarity_calls) == 1


def test_batch_helper_uses_two_queries_for_occurrences_and_manifests(world):
    prefix = "Unicode 🎯 prefix: "
    quote = "The exact canonical fragment remains visible twice."
    text = prefix + quote
    source_id = _parsed(world, text)
    snapshot = _capture(world, [source_id], quote)
    citation = _citation(world, snapshot, source_id, text, quote)
    reordered_manifest = dict(reversed(tuple(snapshot["manifest"].items())))
    records = [
        (snapshot["manifest"], [citation, citation]),
        (reordered_manifest, [citation]),
        (None, []),
    ]

    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            with conn.cursor(row_factory=dict_row) as cur:
                canonical = grounding.validate_citation(cur, snapshot["manifest"], citation)
            with conn.cursor(row_factory=dict_row) as raw_cur:
                counting = _CountingCursor(raw_cur)
                validated = grounding.validate_graph_citations(counting, records)

    assert validated == [[canonical, canonical], [canonical], []]
    assert len(counting.statements) == 2
    assert canonical["quote"] == quote
    assert canonical["start"] == len(prefix)
    assert canonical["text_hash"] == hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(
    "corruption",
    [
        pytest.param("id", id="id"),
        pytest.param("quote", id="quote"),
        pytest.param("text_hash", id="hash"),
        pytest.param("offset", id="offset"),
        pytest.param("source_version_id", id="version"),
    ],
)
def test_forged_occurrence_cannot_borrow_another_valid_pin(world, corruption):
    _, snapshot, citations = _two_pin_material(world)
    forged = dict(citations[0])
    if corruption == "id":
        forged["id"] = citations[1]["id"]
    elif corruption == "quote":
        forged["quote"] = forged["quote"] + " fabricated"
    elif corruption == "text_hash":
        forged["text_hash"] = "f" * 64
    elif corruption == "offset":
        forged["start"] = forged["start"] + 1
    elif corruption == "source_version_id":
        forged["source_version_id"] = citations[1]["source_version_id"]

    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            with conn.cursor(row_factory=dict_row) as cur:
                with pytest.raises(ValueError):
                    grounding.validate_graph_citations(
                        cur,
                        [(snapshot["manifest"], [citations[0], forged])],
                    )


def test_empty_citations_still_reject_revoked_uncited_dependencies(world, monkeypatch):
    dependency_id, snapshot, _ = _two_pin_material(world)
    concept_id = _insert_concept(
        world,
        "Uncited complete manifest",
        snapshot["manifest"],
        [],
    )
    assert _manifest_readable(world, world["principal"], snapshot["manifest"])
    with psycopg.connect(world["dsn"]) as conn:
        detail = semantic.get_concept(
            conn,
            world["principal"],
            world["lib"],
            concept_id,
        )
    assert detail["evidence"] == []

    _revoke_reader(world, dependency_id, world["principal"])
    assert not _manifest_readable(world, world["principal"], snapshot["manifest"])
    assert not _concept_visible(world, world["principal"], world["lib"], concept_id)

    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            with conn.cursor(row_factory=dict_row) as cur:
                with pytest.raises(AccessDenied):
                    grounding.validate_graph_citations(
                        cur,
                        [
                            (snapshot["manifest"], []),
                            (snapshot["manifest"], []),
                        ],
                    )

    _grant_reader(world, dependency_id, world["principal"])
    assert _concept_visible(world, world["principal"], world["lib"], concept_id)

    def revoke_during_similarity(*args, **kwargs):
        _revoke_reader(world, dependency_id, world["principal"])
        return _unavailable_similarity()

    monkeypatch.setattr(similarity, "similarity_layer", revoke_during_similarity)
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied):
            semantic.semantic_network(
                conn,
                world["principal"],
                world["lib"],
                limit=200,
            )


def test_revoke_after_canonical_fetch_rejects_final_manifest_check(world, monkeypatch):
    dependency_id, snapshot, citations = _two_pin_material(world)
    assert _manifest_readable(world, world["principal"], snapshot["manifest"])
    events = []
    fired = []
    real_validate_fields = grounding._validate_citation_fields

    def revoke_after_validation(*args, **kwargs):
        result = real_validate_fields(*args, **kwargs)
        events.append("validated")
        if not fired:
            _revoke_reader(world, dependency_id, world["principal"])
            fired.append(dependency_id)
        return result

    monkeypatch.setattr(grounding, "_validate_citation_fields", revoke_after_validation)
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            with conn.cursor(row_factory=dict_row) as raw_cur:
                counting = _CountingCursor(raw_cur, events)
                with pytest.raises(AccessDenied):
                    grounding.validate_graph_citations(
                        counting,
                        [(snapshot["manifest"], [citations[0]])],
                    )

    assert fired == [dependency_id]
    assert events[0] == "execute"
    assert events.index("validated") > 0
    assert events[-1] == "execute"
    assert events.count("execute") == 2
    assert len(counting.statements) == 2
    assert not _manifest_readable(world, world["principal"], snapshot["manifest"])


def test_graph_does_not_reuse_access_across_requests_after_revoke(world, monkeypatch):
    dependency_id, snapshot, citations = _two_pin_material(world)
    concept_id = _insert_concept(
        world,
        "Cross-request graph evidence",
        snapshot["manifest"],
        [citations[0]],
        publish=True,
    )
    _grant_reader(world, world["lib"], world["other"])
    _grant_reader(world, dependency_id, world["other"])
    assert _manifest_readable(world, world["other"], snapshot["manifest"])
    monkeypatch.setattr(similarity, "similarity_layer", _unavailable_similarity)

    with psycopg.connect(world["dsn"]) as conn:
        first_graph = semantic.semantic_network(
            conn,
            world["other"],
            world["lib"],
            limit=200,
        )
    first_node = next(node for node in first_graph["nodes"] if node["entity_id"] == concept_id)
    assert first_node["evidence"] == [citations[0]]
    assert _concept_visible(world, world["other"], world["lib"], concept_id)

    _revoke_reader(world, dependency_id, world["other"])
    assert not _manifest_readable(world, world["other"], snapshot["manifest"])
    assert not _concept_visible(world, world["other"], world["lib"], concept_id)

    with psycopg.connect(world["dsn"]) as second_conn:
        second_graph = semantic.semantic_network(
            second_conn, world["other"], world["lib"], limit=200
        )
        assert all(node["entity_id"] != concept_id for node in second_graph["nodes"])
        with pytest.raises(AccessDenied):
            semantic.get_concept(second_conn, world["other"], world["lib"], concept_id)


def test_manifest_registry_deduplicates_and_missing_fact_revalidates(world):
    source_id = _parsed(world, "Unicode 🎯 provenance remains canonical.")
    snapshot = _capture(world, [source_id])
    first_id = _insert_concept(world, "Registry first", snapshot["manifest"], [])
    _insert_concept(world, "Registry duplicate", snapshot["manifest"], [])
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            assert conn.execute(
                "SELECT count(*) FROM kb.semantic_manifest WHERE library_id=%s",
                (world["lib"],),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT has_table_privilege(current_user,'kb.semantic_manifest','UPDATE'),"
                "has_table_privilege(current_user,'kb.semantic_manifest','DELETE')"
            ).fetchone() == (False, False)
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                forged = {**snapshot["manifest"], "sources": []}
                conn.execute(
                    "INSERT INTO kb.semantic_manifest(library_id,manifest) VALUES(%s,%s)",
                    (world["lib"], Jsonb(forged)),
                )
        assert semantic.get_concept(conn, world["principal"], world["lib"], first_id)

    # Registry loss uses the original live authority, never a stale permission.
    # Rebuild only these fixture facts; canonical manifests never change.
    with psycopg.connect(world["admin"]) as admin:
        assert (
            admin.execute(
                "DELETE FROM kb.semantic_manifest WHERE library_id=%s", (world["lib"],)
            ).rowcount
            == 1
        )
    assert _manifest_readable(world, world["principal"], snapshot["manifest"])
    with psycopg.connect(world["dsn"]) as conn:
        assert semantic.get_concept(conn, world["principal"], world["lib"], first_id)
    with psycopg.connect(world["admin"]) as admin:
        previous = admin.execute(
            "SELECT publication FROM kb.source WHERE id=%s", (source_id,)
        ).fetchone()[0]
        admin.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (source_id,))
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied):
            semantic.get_concept(conn, world["principal"], world["lib"], first_id)
    with psycopg.connect(world["admin"]) as admin:
        admin.execute("UPDATE kb.source SET publication=%s WHERE id=%s", (previous, source_id))
        admin.execute(
            "INSERT INTO kb.semantic_manifest(library_id,manifest) "
            "SELECT DISTINCT library_id,manifest FROM kb.semantic_concept "
            "WHERE library_id=%s AND manifest IS NOT NULL "
            "ON CONFLICT(library_id,manifest_hash) DO NOTHING",
            (world["lib"],),
        )
    with psycopg.connect(world["dsn"]) as conn:
        assert semantic.get_concept(conn, world["principal"], world["lib"], first_id)
