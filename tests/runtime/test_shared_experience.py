"""Explicit shared experience never widens the private use/outcome audience."""

import hashlib
import json

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_final_project_manifest import _published
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import libraries, shared_experience, writes

pytestmark = pytest.mark.integration


def _target(conn, owner):
    return libraries.create_library(
        conn,
        owner,
        libraries.CreateLibrary(
            organisation_id=owner.account_id, name="Synthetic project experience", kind="project"
        ),
    ).id


def _grant(conn, owner, library_id, other, role="reader"):
    libraries.set_grant(
        conn, owner, library_id, libraries.SetGrant(principal_id=other.principal_id, role=role)
    )


def _case(conn, principal, rule, *, unknown=False):
    use = writes.start_use(
        conn, principal, rule["id"], rule["version_no"], "Synthetic expectation", "Synthetic pilot"
    )
    writes.mark_applied(conn, principal, use["id"])
    outcome = writes.record_outcome(
        conn,
        principal,
        use["id"],
        observed_result="Synthetic observation" if not unknown else None,
        rating=None if unknown else 4,
        rating_basis=None if unknown else "Synthetic self report",
        achievement="unknown" if unknown else "met",
        assessor_kind="unknown" if unknown else "agent",
    )
    return use, outcome


def _empty(conn, principal, target):
    result = shared_experience.list_shared(conn, principal, target)
    assert result["items"] == result["by_rule_version"] == []
    return result


def test_explicit_publication_preserves_private_corrections_and_latest_snapshot(world):
    owner, other = world["principal"], world["other"]
    rule, _ = _published(world, world["lib"], "Synthetic public communication rule")
    with psycopg.connect(world["dsn"]) as conn:
        target = _target(conn, owner)
        for lib in [target, world["lib"]]:
            _grant(conn, owner, lib, other)
        use, outcome = _case(conn, owner, rule)
        assert writes.list_uses(conn, other) == []
        _empty(conn, other, target)
        first = shared_experience.publish(conn, owner, target, use["id"], outcome["id"])
        assert shared_experience.publish(conn, owner, target, use["id"], outcome["id"]) == first
        canonical = json.dumps(
            first["snapshot"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        assert first["content_hash"] == hashlib.sha256(canonical.encode()).hexdigest()
        assert first["snapshot"]["expected_result"] == "Synthetic expectation"
        correction = writes.record_outcome(
            conn,
            owner,
            use["id"],
            "Later private synthetic correction",
            1,
            "Synthetic self report",
            is_correction=True,
            achievement="not_met",
            assessor_kind="human",
        )
        unchanged = shared_experience.list_shared(conn, other, target)
        assert [x["snapshot"] for x in unchanged["items"]] == [first["snapshot"]]
        assert unchanged["by_rule_version"][0]["average_rating"] == 4
        second = shared_experience.publish(conn, owner, target, use["id"], correction["id"])
        assert second["id"] != first["id"]
        # Republishing the older event must not replace the latest reported snapshot.
        assert shared_experience.publish(conn, owner, target, use["id"], outcome["id"]) == first
        other_use, older = _case(conn, owner, rule)
        unknown = writes.record_outcome(
            conn, owner, other_use["id"], is_correction=True, achievement="unknown"
        )
        third = shared_experience.publish(conn, owner, target, other_use["id"], unknown["id"])
        # Publishing the earlier report last must not make it the latest case.
        fourth = shared_experience.publish(conn, owner, target, other_use["id"], older["id"])
        listed = shared_experience.list_shared(conn, other, target)
        assert listed["basis"] == "explicit_shared_self_report_not_business_proof"
        assert {x["id"] for x in listed["items"]} == {
            first["id"],
            second["id"],
            third["id"],
            fourth["id"],
        }
        assert (
            next(x for x in listed["items"] if x["id"] == first["id"])["snapshot"]
            == first["snapshot"]
        )
        assert len(listed["by_rule_version"]) == 1
        aggregate = listed["by_rule_version"][0]
        assert (aggregate["rule_id"], aggregate["rule_version_no"]) == (rule["id"], 1)
        assert (aggregate["total"], aggregate["rated"], aggregate["average_rating"]) == (2, 1, 1)
        assert aggregate["achievement_counts"] == {
            "met": 0,
            "partly": 0,
            "not_met": 1,
            "unknown": 1,
        }
        assert aggregate["assessor_kind_counts"] == {
            "human": 1,
            "agent": 0,
            "measurement": 0,
            "unknown": 1,
        }
        assert writes.list_uses(conn, other) == []
    with psycopg.connect(world["admin"]) as conn:
        for item in listed["items"]:
            stored = conn.execute(
                "SELECT principal_id,item_kind,item_id FROM kb.retrieval_event WHERE id=%s",
                (item["retrieval_event_id"],),
            ).fetchone()
            assert stored == (other.principal_id, "experience", item["id"])
        assert (
            conn.execute(
                "SELECT snapshot FROM kb.shared_experience WHERE id=%s", (first["id"],)
            ).fetchone()[0]
            == first["snapshot"]
        )


def test_shared_visibility_requires_both_grants_and_canonical_authority(world):
    owner, other = world["principal"], world["other"]
    rule, _ = _published(world, world["lib"], "Synthetic origin with separate audience")
    with psycopg.connect(world["dsn"]) as conn:
        target = _target(conn, owner)
        use, outcome = _case(conn, owner, rule)
        published = shared_experience.publish(conn, owner, target, use["id"], outcome["id"])
        _grant(conn, owner, target, other)
        _empty(conn, other, target)  # Target alone does not reveal origin material.
        _grant(conn, owner, world["lib"], other)
        assert (
            shared_experience.list_shared(conn, other, target)["items"][0]["id"] == published["id"]
        )
        libraries.revoke_grant(conn, owner, world["lib"], other.principal_id)
        _empty(conn, other, target)
        _grant(conn, owner, world["lib"], other)
        libraries.revoke_grant(conn, owner, target, other.principal_id)
        _empty(conn, other, target)
        # A non-admin author still needs curator on the destination.
        own_use, own_outcome = _case(conn, other, rule)
        for role in ["reader", "contributor"]:
            _grant(conn, owner, target, other, role)
            with pytest.raises(AccessDenied):
                shared_experience.publish(conn, other, target, own_use["id"], own_outcome["id"])
        _grant(conn, owner, target, other, "curator")
        with pytest.raises(AccessDenied):
            shared_experience.publish(conn, other, target, use["id"], outcome["id"])
        own = shared_experience.publish(conn, other, target, own_use["id"], own_outcome["id"])
        _grant(conn, owner, target, other, "contributor")
        with pytest.raises(AccessDenied):
            shared_experience.publish(conn, other, target, own_use["id"], own_outcome["id"])
        _grant(conn, owner, target, other, "curator")
        # SQL RLS rejects caller-provided publisher or altered canonical snapshot.
        # A fresh unpublished key prevents uniqueness from masking an RLS failure.
        raw_use, raw_outcome = _case(conn, other, rule)
        with transaction_identity(conn, other):
            canonical = conn.execute(
                "SELECT kb.experience_snapshot(%s,%s)", (raw_use["id"], raw_outcome["id"])
            ).fetchone()[0]
        for publisher, snapshot in [
            (owner.principal_id, canonical),
            (other.principal_id, {**canonical, "observed_result": "Forged synthetic result"}),
        ]:
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                transaction_identity(conn, other),
            ):
                conn.execute(
                    "INSERT INTO kb.shared_experience(library_id,use_id,outcome_id,rule_id,"
                    "rule_version_no,publisher_id,snapshot) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        target,
                        raw_use["id"],
                        raw_outcome["id"],
                        rule["id"],
                        1,
                        publisher,
                        Jsonb(snapshot),
                    ),
                )
        for statement in [
            "UPDATE kb.shared_experience SET snapshot='{}'::jsonb WHERE id=%s",
            "DELETE FROM kb.shared_experience WHERE id=%s",
        ]:
            with pytest.raises(psycopg.Error), transaction_identity(conn, other):
                conn.execute(statement, (own["id"],))
        libraries.revoke_grant(conn, owner, world["lib"], other.principal_id)
        with pytest.raises(AccessDenied):
            shared_experience.publish(conn, other, target, own_use["id"], own_outcome["id"])
        _grant(conn, owner, world["lib"], other)
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "UPDATE kb.membership SET status='deactivated',deactivated_at=now(),deactivated_by=%s "
            "WHERE organisation_id=%s AND principal_id=%s",
            (owner.principal_id, owner.account_id, other.principal_id),
        )
    with psycopg.connect(world["dsn"]) as conn:
        _empty(conn, other, target)
        with pytest.raises(AccessDenied):
            shared_experience.publish(conn, other, target, own_use["id"], own_outcome["id"])
