"""Independent PostgreSQL acceptance of workspace review, views and exports."""

import hashlib
import io
import json
from uuid import uuid4

import psycopg
import pytest
from test_research_grounding import parsed
from test_research_product import execute
from test_runtime import rule, source
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import content, libraries, notes, policies, upload, workspace, writes
from kb.ingestion import submit_processing

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]


def draft_knowledge(world, principal, library_id, ident, statement="Original claim"):
    with psycopg.connect(world["dsn"]) as conn, transaction_identity(conn, principal):
        conn.execute(
            "INSERT INTO kb.knowledge(id,library_id,statement,kind) VALUES(%s,%s,%s,'assertion')",
            (ident, library_id, statement),
        )


def extra_library(world, principal, name):
    with psycopg.connect(world["dsn"]) as conn:
        return libraries.create_library(
            conn,
            principal,
            libraries.CreateLibrary(
                organisation_id=principal.account_id, name=name, kind="reference"
            ),
        ).id


def grant_other(world, role):
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,%s)",
            (world["lib"], world["other"].principal_id, role),
        )


@pytest.mark.parametrize("invalid", ["another_library", "missing_item"])
def test_batch_rejection_rolls_back_an_earlier_valid_publication(world, invalid):
    first_id, later_id = sorted([uuid4(), uuid4()])
    draft_knowledge(world, world["principal"], world["lib"], first_id)
    if invalid == "another_library":
        other_library = extra_library(world, world["principal"], "Other batch scope")
        draft_knowledge(world, world["principal"], other_library, later_id)
    batch = workspace.ReviewBatch(
        item_kind="knowledge", item_ids=[str(later_id), str(first_id)], decision="publish"
    )
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied):
            workspace.review_batch(conn, world["principal"], world["lib"], batch)
        with transaction_identity(conn, world["principal"]):
            assert conn.execute(
                "SELECT publication FROM kb.knowledge WHERE id=%s", (first_id,)
            ).fetchone() == ("draft",)
        # A successful retry with the intended scope publishes exactly one item.
        valid = workspace.ReviewBatch(
            item_kind="knowledge", item_ids=[str(first_id), str(first_id)], decision="publish"
        )
        assert workspace.review_batch(conn, world["principal"], world["lib"], valid) == {
            "reviewed": 1,
            "publication": "published",
        }
        assert (
            workspace.review_batch(conn, world["principal"], world["lib"], valid)["reviewed"] == 1
        )


def test_incomplete_rule_rolls_back_the_entire_batch(world):
    first_id, later_id = sorted([uuid4(), uuid4()])
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            for ident in [first_id, later_id]:
                conn.execute(
                    "INSERT INTO kb.rule(id,library_id,version_no,title,"
                    "when_to_apply,expected_effect) "
                    "VALUES(%s,%s,1,'Review draft','Before work','Measured result')",
                    (ident, world["lib"]),
                )
            conn.execute(
                "INSERT INTO kb.rule_action(rule_id,ordinal,body) VALUES(%s,0,'Measure')",
                (first_id,),
            )
        batch = workspace.ReviewBatch(
            item_kind="rule", item_ids=[str(first_id), str(later_id)], decision="publish"
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="incomplete rule"):
            workspace.review_batch(conn, world["principal"], world["lib"], batch)
        assert {
            row["id"]: row["publication"]
            for row in content.list_rules(conn, world["principal"], world["lib"])
        } == {first_id: "draft", later_id: "draft"}


def test_contributor_cannot_batch_publish_their_own_draft(world):
    grant_other(world, "contributor")
    ident = uuid4()
    draft_knowledge(world, world["other"], world["lib"], ident)
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied):
            workspace.review_batch(
                conn,
                world["other"],
                world["lib"],
                workspace.ReviewBatch(
                    item_kind="knowledge", item_ids=[str(ident)], decision="publish"
                ),
            )
        with transaction_identity(conn, world["other"]):
            assert conn.execute(
                "SELECT publication FROM kb.knowledge WHERE id=%s", (ident,)
            ).fetchone() == ("draft",)


def test_knowledge_edit_rejects_stale_text_foreign_author_and_published_mutation(world):
    grant_other(world, "contributor")
    owner_id, own_id = uuid4(), uuid4()
    original = "Точный исходный текст 🎯"
    draft_knowledge(world, world["principal"], world["lib"], owner_id, original)
    draft_knowledge(world, world["other"], world["lib"], own_id, original)
    body = workspace.EditKnowledge(
        statement="Исправленный текст", expected_statement=original, kind="recommendation"
    )
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied):
            workspace.edit_knowledge(conn, world["other"], owner_id, body)
        updated = workspace.edit_knowledge(conn, world["other"], own_id, body)
        assert (updated["statement"], updated["kind"], updated["created_by"]) == (
            body.statement,
            "recommendation",
            world["other"].principal_id,
        )
        with pytest.raises(ValueError, match="knowledge_revision_changed"):
            workspace.edit_knowledge(conn, world["other"], own_id, body)
        with pytest.raises(ValueError, match="knowledge_revision_changed"):
            workspace.edit_knowledge(
                conn,
                world["other"],
                own_id,
                workspace.EditKnowledge(
                    statement="Must not be stored",
                    expected_statement=body.statement + " ",
                    kind="assertion",
                ),
            )
        writes.publish(conn, world["principal"], own_id, knowledge=True)
        with pytest.raises(AccessDenied):
            workspace.edit_knowledge(
                conn,
                world["principal"],
                own_id,
                workspace.EditKnowledge(
                    statement="Forbidden revision",
                    expected_statement=body.statement,
                    kind="example",
                ),
            )
        visible = writes.list_knowledge(conn, world["other"], world["lib"])
        assert [(row["statement"], row["publication"]) for row in visible] == [
            (body.statement, "published")
        ]


def test_job_list_does_not_leak_another_principals_private_library_on_repeat(world):
    own_source = source(world, "Visible source for a queued job.")
    private_library = extra_library(world, world["other"], "private-job-canary")
    with psycopg.connect(world["dsn"]) as conn:
        own_job = submit_processing(conn, world["principal"], own_source, str(uuid4()))
        private_source = upload.submit_file(
            conn,
            world["other"],
            world["store"],
            upload.SubmitFile(
                library_id=private_library,
                title="private-source-canary",
                idempotency_key=str(uuid4()),
            ),
            io.BytesIO(b"Private source bytes."),
        ).source.id
        private_job = submit_processing(conn, world["other"], private_source, str(uuid4()))
        for _ in range(2):
            visible = workspace.list_jobs(conn, world["principal"])
            assert [row["id"] for row in visible] == [own_job["job_id"]]
            assert "private-source-canary" not in str(visible)
            assert workspace.list_jobs(conn, world["principal"], private_library) == []
        assert [row["id"] for row in workspace.list_jobs(conn, world["other"])] == [
            private_job["job_id"]
        ]


def test_author_cannot_move_a_draft_to_foreign_library_or_delete_curator_publication(world):
    grant_other(world, "contributor")
    ident = uuid4()
    draft_knowledge(world, world["other"], world["lib"], ident)
    foreign = extra_library(world, world["principal"], "No author grant")
    with psycopg.connect(world["dsn"]) as conn:
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            transaction_identity(conn, world["other"]),
        ):
            conn.execute("UPDATE kb.knowledge SET library_id=%s WHERE id=%s", (foreign, ident))
        writes.publish(conn, world["principal"], ident, knowledge=True)
        with (
            pytest.raises(psycopg.errors.CheckViolation),
            transaction_identity(conn, world["other"]),
        ):
            conn.execute("DELETE FROM kb.knowledge WHERE id=%s", (ident,))
        assert (
            content.search_knowledge(conn, world["other"], "Original claim")["hits"][0]["id"]
            == ident
        )


def test_experience_summary_is_own_exact_version_and_keeps_unknown_ratings(world):
    grant_other(world, "reader")
    original = rule(world)
    private_library = extra_library(world, world["other"], "private-experience-canary")
    with psycopg.connect(world["dsn"]) as conn:
        next_version = writes.create_rule(
            conn,
            world["principal"],
            world["lib"],
            writes.RuleInput(
                title="Experiment version two",
                when_to_apply="Before work",
                expected_effect="Measured result",
                actions=["Measure again"],
            ),
            replaces=original["id"],
        )
        writes.publish(conn, world["principal"], next_version["id"])
        writes.start_use(conn, world["principal"], original["id"], 1)
        unknown_rating = writes.start_use(conn, world["principal"], original["id"], 1)
        writes.mark_applied(conn, world["principal"], unknown_rating["id"])
        writes.record_outcome(conn, world["principal"], unknown_rating["id"], "Reported, no rating")
        rated = writes.start_use(conn, world["principal"], next_version["id"], 2)
        writes.mark_applied(conn, world["principal"], rated["id"])
        writes.record_outcome(conn, world["principal"], rated["id"], "First report", 4)
        writes.record_outcome(
            conn, world["principal"], rated["id"], "Correction", 5, is_correction=True
        )
        foreign = writes.start_use(conn, world["other"], original["id"], 1)
        writes.mark_applied(conn, world["other"], foreign["id"])
        writes.record_outcome(conn, world["other"], foreign["id"], "private-report-canary", 1)
        hidden_rule = writes.create_rule(
            conn,
            world["other"],
            private_library,
            writes.RuleInput(
                title="private-experience-canary",
                when_to_apply="Privately",
                expected_effect="Private result",
                actions=["Private action"],
            ),
        )
        writes.publish(conn, world["other"], hidden_rule["id"])
        writes.start_use(conn, world["other"], hidden_rule["id"], 1)
        for _ in range(2):
            result = workspace.experience_summary(conn, world["principal"])
            assert result["basis"] == "own_reported_experience"
            rows = {row["rule_version_no"]: row for row in result["by_rule_version"]}
            assert set(rows) == {1, 2}
            assert rows[1]["rule_id"] == original["id"]
            assert rows[2]["rule_id"] == next_version["id"]
            assert {
                key: rows[1][key]
                for key in ["retrieved", "applied", "reported", "outcome_unknown", "rated"]
            } == {"retrieved": 2, "applied": 1, "reported": 1, "outcome_unknown": 1, "rated": 0}
            assert rows[1]["average_rating"] is None
            assert rows[2]["reported"] == 1 and rows[2]["average_rating"] == 5
            assert "private-experience-canary" not in str(result)
            assert "private-report-canary" not in str(result)


def test_skill_manifest_pins_exact_rule_version_and_source_hash(world):
    text = "An exact synthetic source for export provenance."
    source_id = parsed(world, text)
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            fragment = conn.execute(
                "SELECT id FROM kb.fragment WHERE source_id=%s", (source_id,)
            ).fetchone()[0]
        original = rule(world, fragment)
        revised = writes.create_rule(
            conn,
            world["principal"],
            world["lib"],
            writes.RuleInput(
                title="Later exported version",
                when_to_apply="Before work",
                expected_effect="Measured result",
                actions=["Measure again"],
                fragment_ids=[fragment],
            ),
            replaces=original["id"],
        )
        writes.publish(conn, world["principal"], revised["id"])
        exported = content.export_skill(conn, world["principal"], world["lib"], [original["id"]])
        manifest = exported["manifest"]
        assert manifest["rules"] == [
            {"id": str(original["id"]), "lineage_id": str(original["id"]), "version_no": 1}
        ]
        assert [item["id"] for item in manifest["sources"]] == [str(source_id)]
        assert manifest["sources"][0]["content_hash"] == hashlib.sha256(text.encode()).hexdigest()
        assert (
            manifest["content_sha256"] == hashlib.sha256(exported["content"].encode()).hexdigest()
        )
        canonical_manifest = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        assert (
            exported["manifest_sha256"] == hashlib.sha256(canonical_manifest.encode()).hexdigest()
        )
        newer = content.export_skill(conn, world["principal"], world["lib"], [revised["id"]])
        assert newer["manifest"]["rules"] == [
            {"id": str(revised["id"]), "lineage_id": str(original["id"]), "version_no": 2}
        ]


def test_export_policy_closes_original_skill_and_research_but_preserves_local_read(
    world, monkeypatch
):
    # Existing helper uses an explicitly fake HTTP provider; no external/model call.
    run_id, source_id = execute(world, monkeypatch)
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            fragment = conn.execute(
                "SELECT id FROM kb.fragment WHERE source_id=%s", (source_id,)
            ).fetchone()[0]
        published = rule(world, fragment)
        assert upload.read_original(conn, world["principal"], world["store"], source_id).content
        assert content.export_skill(conn, world["principal"], world["lib"], [published["id"]])[
            "content"
        ]
        assert notes.export_research(conn, world["principal"], run_id)["markdown"]
        policies.put_policy(
            conn,
            world["principal"],
            world["lib"],
            policies.LibraryPolicyInput(
                external_processing_allowed=True, cloud_mcp_allowed=False, export_allowed=False
            ),
        )
        with pytest.raises(AccessDenied):
            upload.read_original(conn, world["principal"], world["store"], source_id)
        with pytest.raises(AccessDenied):
            upload.describe_original(conn, world["principal"], source_id)
        with pytest.raises(AccessDenied):
            content.export_skill(conn, world["principal"], world["lib"], [published["id"]])
        with pytest.raises(AccessDenied):
            notes.export_research(conn, world["principal"], run_id)
        assert content.read_source(conn, world["principal"], source_id)["fragments"]
        assert content.get_rule(conn, world["principal"], published["id"])["id"] == published["id"]
