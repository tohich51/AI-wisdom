import hashlib
import json

import psycopg
import pytest
from test_research_grounding import parsed
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import transaction_identity
from kb.catalog import content, libraries, projects, writes

pytestmark = pytest.mark.integration


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _project(conn, owner):
    spec = projects.CreateProject(organisation_id=owner.account_id, name="Public launch")
    return projects.create_project(conn, owner, spec).project.id


def _published(world, library_id, title, replaces=None):
    sid = parsed({**world, "lib": library_id}, f"{title}. State the audience before drafting.")
    with psycopg.connect(world["dsn"]) as conn:
        source = content.read_source(conn, world["principal"], sid)
        fid = source["fragments"][0]["id"]
        body = writes.RuleInput(
            title=title,
            when_to_apply="Before drafting",
            expected_effect="A clear public brief",
            inputs=["Audience"],
            preconditions=["Brief approved"],
            actions=["State the purpose"],
            exceptions=["Escalate conflict"],
            fragment_ids=[fid],
        )
        rule = writes.create_rule(conn, world["principal"], library_id, body, replaces=replaces)
        writes.publish(conn, world["principal"], rule["id"])
    return rule, (sid, fid, source["source"]["content_hash"])


def _brand(conn, owner):
    spec = libraries.CreateLibrary(organisation_id=owner.account_id, name="Brand", kind="brand")
    return libraries.create_library(conn, owner, spec)


def _pin(conn, owner, project, r, priority=0):
    spec = projects.PinRule(rule_id=r["id"], version_no=r["version_no"], priority=priority)
    projects.pin_rule(conn, owner, project, spec)


def test_manifest_preserves_complete_ordered_pinned_context(world):
    with psycopg.connect(world["dsn"]) as conn:
        owner, project = world["principal"], _project(conn, world["principal"])
        brand = _brand(conn, owner).id
        made = []
        for library_id, priority in [(brand, -100), (project, 0), (world["lib"], 999)]:
            if library_id != project:
                link = projects.LinkLibrary(library_id=library_id)
                projects.link_library(conn, owner, project, link)
            rule, evidence = _published(world, library_id, str(library_id))
            _pin(conn, owner, project, rule, priority)
            made.append((rule, evidence))
        first = projects.project_context(conn, owner, project)
        second = projects.project_context(conn, owner, project)
        assert first.complete and first.context_state == "complete"
        assert [pin.layer for pin in first.pins] == ["brand", "project", "general"]
        for pin, (rule, evidence) in zip(first.pins, made, strict=True):
            assert (pin.rule_id, pin.rule.id, pin.version_no) == (rule["id"], rule["id"], 1)
            assert pin.rule.inputs == ["Audience"] and pin.rule.actions == ["State the purpose"]
            assert pin.rule.preconditions == ["Brief approved"]
            assert pin.rule.exceptions == ["Escalate conflict"] and pin.is_required
            ref = pin.rule.provenance[0]
            assert (ref.source_id, ref.fragment_id, ref.content_hash) == evidence
            assert pin.content_hash == _hash(pin.rule.model_dump(mode="json"))
            with transaction_identity(conn, owner):
                sql = (
                    "SELECT created_at FROM kb.project_rule_pin WHERE project_id=%s AND rule_id=%s"
                )
                bound = conn.execute(sql, (project, rule["id"])).fetchone()[0]
            assert pin.bound_at == bound
        assert first.model_dump() == second.model_dump()
        result = first.to_mcp_result()
        assert result.manifest == first.model_dump(mode="json", exclude_none=True)
        assert len(result.manifest["links"]) == 2 and len(result.manifest["pins"]) == 3
        assert result.manifest_sha256 == _hash(result.manifest)


def test_conflicting_exact_pins_do_not_rewrite_original_use(world):
    with psycopg.connect(world["dsn"]) as conn:
        owner, project = world["principal"], _project(conn, world["principal"])
        first, _ = _published(world, world["lib"], "Original public rule")
        use = writes.start_use(conn, owner, first["id"], 1)
        replacement, _ = _published(world, world["lib"], "Revised public rule", first["id"])
        _pin(conn, owner, project, first)
        _pin(conn, owner, project, replacement)
        conflicted = projects.project_context(conn, owner, project)
        assert not conflicted.complete and conflicted.context_state == "conflicted"
        assert {item["code"] for item in conflicted.conflicts} == {"multiple_versions_pinned"}
        expected = {str(first["id"]), str(replacement["id"])}
        assert set(conflicted.conflicts[0]["rule_ids"]) == expected
        assert "pinned_version_conflict" in conflicted.to_mcp_result().unresolved_dependencies
        projects.unpin_rule(conn, owner, project, first["id"])
        resolved = projects.project_context(conn, owner, project)
        assert resolved.complete and resolved.context_state == "complete" and not resolved.conflicts
        assert [(pin.rule_id, pin.version_no) for pin in resolved.pins] == [(replacement["id"], 2)]
        with transaction_identity(conn, owner):
            stored = conn.execute(
                "SELECT rule_id,rule_version_no FROM kb.use_record WHERE id=%s", (use["id"],)
            ).fetchone()
        assert stored == (first["id"], 1)


def test_project_roles_never_reveal_revoked_brand_pin(world):
    with psycopg.connect(world["dsn"]) as conn:
        owner, other = world["principal"], world["other"]
        project = _project(conn, owner)
        brand = _brand(conn, owner)
        hidden, evidence = _published(world, brand.id, "Hidden synthetic design covenant")
        _pin(conn, owner, project, hidden)
        projects.link_library(conn, owner, project, projects.LinkLibrary(library_id=brand.id))
        grant = libraries.SetGrant(principal_id=other.principal_id, role="reader")
        for lib in [brand.id, project]:
            libraries.set_grant(conn, owner, lib, grant)
        visible = projects.project_context(conn, other, project)
        assert visible.complete and visible.pins[0].rule.id == hidden["id"]
        hidden_hash = visible.pins[0].content_hash
        libraries.revoke_grant(conn, owner, brand.id, other.principal_id)
        for role in ["reader", "curator"]:
            grant = libraries.SetGrant(principal_id=other.principal_id, role=role)
            libraries.set_grant(conn, owner, project, grant)
            for _ in range(2):
                blocked = projects.project_context(conn, other, project)
                assert not blocked.complete and blocked.context_state == "blocked"
                pin = blocked.pins[0]
                assert pin.status == "blocked"
                fields = ("rule", "rule_id", "lineage_id", "layer", "content_hash")
                assert all(getattr(pin, name) is None for name in fields)
                result = blocked.to_mcp_result()
                assert result.pinned_rules == result.manifest["pins"] == []
                assert "pinned_rule_access_missing" in result.unresolved_dependencies
                serialized = result.model_dump_json()
                hidden_values = [hidden[k] for k in ("title", "id", "lineage_id")]
                secrets = [*hidden_values, brand.id, brand.name, *evidence, hidden_hash]
                assert all(str(value) not in serialized for value in secrets)
                assert result.manifest["blocked_pin_count"] == 1
