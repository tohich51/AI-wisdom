"""Real PostgreSQL acceptance of shared Research/Studio/graph catalog boundaries."""

from uuid import uuid4

import psycopg
import pytest
from test_research_grounding import parsed
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import graphs, notes, research, research_rules, semantic, writes
from kb.research_worker import run_research

pytestmark = pytest.mark.integration


def execute(world, monkeypatch, kind="research"):
    source_id = parsed(world, "Измерьте результат эксперимента. Сегмент определяет аудиторию.")
    with psycopg.connect(world["dsn"]) as conn:
        submitted = research.submit_research(
            conn,
            world["principal"],
            world["lib"],
            [source_id],
            kind,
            "Как проверить?",
            str(uuid4()),
        )

    def provider(url, **kwargs):
        payload = kwargs["json"]
        fragment = payload["fragments"][0]
        citation = {"fragment_id": fragment["id"], "quote": "Измерьте результат эксперимента."}
        item = {
            "heading": "Проверка",
            "text": citation["quote"],
            "source_id": None,
            "parent_index": None,
            "citations": [citation],
        }
        items = [item]
        rels = []
        if kind == "network":
            items.append({**item, "heading": "Результат"})
            rels = [
                {
                    "from_item": 0,
                    "to_item": 1,
                    "predicate": "used_for",
                    "hypothesis": True,
                    "citations": [citation],
                }
            ]
        import httpx

        return httpx.Response(
            200,
            json={
                "task_id": payload["task_id"],
                "model": "MiniMax-M3.1-Flash-Preview",
                "kind": kind,
                "title": "Измерение",
                "no_evidence": False,
                "items": items,
                "relations": rels,
                "support_verified": True,
                "provider_calls": 2,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("kb.research_worker.httpx.post", provider)
    run_research(
        world["worker"], submitted["run_id"], world["principal"], "http://runner.test", "t" * 32
    )
    return submitted["run_id"], source_id


def test_worker_notes_export_revision_draft_and_private_reads(world, monkeypatch):
    run_id, _ = execute(world, monkeypatch)
    with psycopg.connect(world["dsn"]) as conn:
        result = research.get_research(conn, world["principal"], run_id)
        assert result["status"] == "done", result
        assert result["result"]["citations"][0]["quote"] == "Измерьте результат эксперимента."
        note = notes.save_note(
            conn, world["principal"], run_id, "Эксперимент", "Личный комментарий"
        )["note"]
        assert (
            notes.list_notes(conn, world["principal"], world["lib"])["notes"][0]["id"] == note["id"]
        )
        changed = notes.update_note(
            conn, world["principal"], note["id"], "Проверка", "После чтения", 1
        )
        assert changed["note"]["revision"] == 2
        with pytest.raises(ValueError, match="note_revision_conflict"):
            notes.update_note(conn, world["principal"], note["id"], "Устаревшее", "", 1)
        md = notes.export_research(conn, world["principal"], run_id)
        assert "Измерьте результат" in md["markdown"]
        assert "file_page:" not in md["markdown"]
        assert len(md["sha256"]) == 64
        rule = research_rules.draft_rule_from_research(
            conn, world["principal"], run_id, "Проверить", "До запуска", "Измерение"
        )
        assert rule["publication"] == "draft"
        assert writes.list_uses(conn, world["principal"]) == []
        with pytest.raises(AccessDenied):
            notes.get_note(conn, world["other"], note["id"])
        with pytest.raises(AccessDenied):
            research.get_research(conn, world["other"], run_id)
        with transaction_identity(conn, world["principal"]):
            conn.execute(
                "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
                (world["lib"], world["principal"].principal_id),
            )
        with pytest.raises(AccessDenied):
            notes.get_note(conn, world["principal"], note["id"])


def test_semantic_persist_review_version_and_endpoint_visibility(world, monkeypatch):
    run_id, _ = execute(world, monkeypatch, "network")
    with psycopg.connect(world["dsn"]) as conn:
        assert research.get_research(conn, world["principal"], run_id)["status"] == "done"
        graph = semantic.semantic_network(conn, world["principal"], world["lib"])
        assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1
        assert graph["edges"][0]["hypothesis"] is True
        assert graph["similarity"]["available"] is False
        edge = graph["edges"][0]
        with pytest.raises(ValueError, match="publish_endpoints_first"):
            semantic.review_semantic(
                conn, world["principal"], edge["entity_id"], "relation", "publish"
            )
        for n in graph["nodes"]:
            semantic.review_semantic(conn, world["principal"], n["entity_id"], "concept", "publish")
        semantic.review_semantic(conn, world["principal"], edge["entity_id"], "relation", "publish")
        corrected = semantic.create_concept(
            conn,
            world["principal"],
            world["lib"],
            "Новая версия",
            "Новое описание",
            graph["nodes"][0]["entity_id"],
        )
        assert corrected["version_no"] == 2 and corrected["publication"] == "draft"
        assert (
            semantic.semantic_network(conn, world["principal"], world["lib"])["edges"][0][
                "publication"
            ]
            == "published"
        )
        semantic.review_semantic(
            conn, world["principal"], graph["nodes"][0]["entity_id"], "concept", "withdraw"
        )
        assert semantic.semantic_network(conn, world["principal"], world["lib"])["edges"] == []
        with pytest.raises(AccessDenied):
            semantic.create_relation(
                conn, world["principal"], world["lib"], corrected["id"], uuid4(), "supports"
            )


def test_provenance_exact_version_and_no_fake_usage(world, monkeypatch):
    run_id, source_id = execute(world, monkeypatch)
    with psycopg.connect(world["dsn"]) as conn:
        draft = research_rules.draft_rule_from_research(
            conn, world["principal"], run_id, "Правило", "До", "После"
        )
        writes.publish(conn, world["principal"], draft["id"])
        graph = graphs.provenance_graph(conn, world["principal"], world["lib"], [source_id])
        assert {"source", "fragment", "rule"} <= {n["kind"] for n in graph["nodes"]}
        assert not {"use", "outcome"} & {n["kind"] for n in graph["nodes"]}
        use = writes.start_use(conn, world["principal"], draft["id"], 1, "Выяснить эффект", "Пилот")
        graph = graphs.provenance_graph(conn, world["principal"], world["lib"])
        assert any(e["predicate"] == "retrieved" for e in graph["edges"])
        writes.mark_applied(conn, world["principal"], use["id"])
        writes.record_outcome(conn, world["principal"], use["id"], "Проверено", 4, "Личный опыт")
        graph = graphs.provenance_graph(conn, world["principal"], world["lib"])
        assert {"use", "outcome"} <= {n["kind"] for n in graph["nodes"]}
        assert all(
            e["source"] in {n["id"] for n in graph["nodes"]}
            and e["target"] in {n["id"] for n in graph["nodes"]}
            for e in graph["edges"]
        )
        assert graphs.provenance_graph(conn, world["principal"], world["lib"], limit=1)["truncated"]


def test_idempotency_and_reader_cannot_generate(world):
    source_id = parsed(world, "Текст для исследования.")
    key = str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        one = research.submit_research(
            conn, world["principal"], world["lib"], [source_id], "research", "Вопрос", key
        )
        two = research.submit_research(
            conn, world["principal"], world["lib"], [source_id], "research", "Вопрос", key
        )
        assert one["run_id"] == two["run_id"] and two["duplicate_of_existing"]
        with pytest.raises(ValueError, match="idempotency_conflict"):
            research.submit_research(
                conn, world["principal"], world["lib"], [source_id], "summary", "", key
            )
        with transaction_identity(conn, world["principal"]):
            conn.execute(
                "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,'reader')",
                (world["lib"], world["other"].principal_id),
            )
        assert (
            research.list_research_sources(conn, world["other"], world["lib"])["can_generate"]
            is False
        )
        with pytest.raises(AccessDenied):
            research.submit_research(
                conn, world["other"], world["lib"], [source_id], "research", "Вопрос", str(uuid4())
            )


def test_generation_revoked_during_provider_io_cannot_commit_network(world, monkeypatch):
    import httpx

    source_id = parsed(world, "Измерьте результат эксперимента.")
    with psycopg.connect(world["dsn"]) as conn:
        submission = research.submit_research(
            conn, world["principal"], world["lib"], [source_id], "network", "", str(uuid4())
        )

    def provider(url, **kwargs):
        with psycopg.connect(world["admin"]) as admin:
            admin.execute(
                "DELETE FROM kb.organisation_admin WHERE organisation_id=%s AND principal_id=%s",
                (world["principal"].account_id, world["principal"].principal_id),
            )
        payload = kwargs["json"]
        fragment = payload["fragments"][0]
        item = {
            "heading": "Эксперимент",
            "text": fragment["text"],
            "source_id": None,
            "parent_index": None,
            "citations": [{"fragment_id": fragment["id"], "quote": fragment["text"]}],
        }
        return httpx.Response(
            200,
            json={
                "task_id": payload["task_id"],
                "model": "MiniMax-M3.1-Flash-Preview",
                "kind": "network",
                "title": "Тест",
                "no_evidence": False,
                "items": [item],
                "relations": [],
                "support_verified": True,
                "provider_calls": 2,
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("kb.research_worker.httpx.post", provider)
    run_research(
        world["worker"], submission["run_id"], world["principal"], "http://runner.test", "t" * 32
    )
    with psycopg.connect(world["dsn"]) as conn:
        result = research.get_research(conn, world["principal"], submission["run_id"])
        assert result["status"] == "failed" and result["error_code"] == "access_revoked"
        assert semantic.semantic_network(conn, world["principal"], world["lib"])["nodes"] == []


def test_concept_revision_keeps_inherited_dependency_acl(world, monkeypatch):
    _, source_id = execute(world, monkeypatch, "network")
    with psycopg.connect(world["dsn"]) as conn:
        graph = semantic.semantic_network(conn, world["principal"], world["lib"])
        revised = semantic.create_concept(
            conn,
            world["principal"],
            world["lib"],
            "Ручная правка",
            "Новый текст",
            graph["nodes"][0]["entity_id"],
        )
        nodes = semantic.semantic_network(conn, world["principal"], world["lib"])["nodes"]
        revision = next(n for n in nodes if n["entity_id"] == revised["id"])
        assert revision["basis"] == "manual_revision_with_inherited_sources"
        assert revision["evidence"]
        with transaction_identity(conn, world["principal"]):
            conn.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (source_id,))
        assert semantic.semantic_network(conn, world["principal"], world["lib"])["nodes"] == []
