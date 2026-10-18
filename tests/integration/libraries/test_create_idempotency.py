"""Focused real-PostgreSQL coverage for create idempotency.

These tests exercise the catalogue functions against kb_app connections. HTTP
library input is covered separately below; the existing content router already
delegates RuleInput directly to create_rule.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from kb.access.policy import AccessDenied, Principal
from kb.catalog import libraries as library_catalog
from kb.catalog import projects as project_catalog
from kb.catalog import writes
from kb.catalog.idempotency import LIBRARY_CREATE, PROJECT_CREATE, RULE_CREATE
from kb.contracts.enums import LibraryKind

pytestmark = pytest.mark.integration


def _actor(world, principal_id, account_id=None) -> Principal:
    return Principal(
        principal_id=principal_id,
        account_id=account_id or world.organisation(),
        generation_watermark=1,
    )


def _library_spec(organisation_id, name="created", key=None):
    return library_catalog.CreateLibrary(
        organisation_id=organisation_id,
        name=name,
        kind=LibraryKind.REFERENCE,
        idempotency_key=key,
    )


def _create_library(db_pool, principal, organisation_id, name="created", key=None):
    with db_pool.connection() as conn:
        return library_catalog.create_library(
            conn,
            principal,
            _library_spec(organisation_id, name, key),
        )


def _project_spec(organisation_id, name="created", description=None, key=None):
    return project_catalog.CreateProject(
        organisation_id=organisation_id,
        name=name,
        description=description,
        idempotency_key=key,
    )


def _create_project(
    db_pool,
    principal,
    organisation_id,
    name="created",
    description=None,
    key=None,
):
    with db_pool.connection() as conn:
        return project_catalog.create_project(
            conn,
            principal,
            _project_spec(organisation_id, name, description, key),
        )


def _race(db_pool, operation):
    barrier = Barrier(2)

    def invoke(_index):
        with db_pool.connection() as conn:
            barrier.wait(timeout=15)
            return operation(conn)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke, index) for index in range(2)]
        return tuple(future.result(timeout=30) for future in futures)


def _rule(key=None, title="one rule", action="do the thing"):
    return writes.RuleInput(
        title=title,
        when_to_apply="when needed",
        expected_effect="the expected effect",
        inputs=["input"],
        preconditions=["precondition"],
        actions=[action],
        exceptions=["known exception"],
        idempotency_key=key,
    )


def _create_rule(db_pool, principal, library_id, body, replaces=None):
    with db_pool.connection() as conn:
        return writes.create_rule(conn, principal, library_id, body, replaces)


def test_http_library_replay_with_the_same_key_returns_one_entity(api, world, people):
    key = str(uuid4())
    payload = {
        "organisation_id": str(world.organisation()),
        "name": "http-idempotent",
        "kind": "reference",
        "audience_scope": "private",
        "idempotency_key": key,
    }
    owner = api.as_(people.owner)
    first = owner.post("/libraries", json=payload)
    second = owner.post("/libraries", json=payload)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert (
        world.count(
            "SELECT count(*) FROM kb.library WHERE organisation_id=%s AND name='http-idempotent'",
            (world.organisation(),),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, LIBRARY_CREATE, key),
        )
        == 1
    )


def test_concurrent_library_creates_with_one_key_return_the_same_library(db_pool, world, people):
    principal = _actor(world, people.owner)
    key = str(uuid4())
    spec = _library_spec(world.organisation(), "one-concurrent-library", key)

    def attempt(conn):
        return library_catalog.create_library(conn, principal, spec)

    first, second = _race(db_pool, attempt)

    assert first.id == second.id
    assert (
        world.count(
            "SELECT count(*) FROM kb.library "
            "WHERE organisation_id=%s AND name='one-concurrent-library'",
            (world.organisation(),),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.library_grant g
           JOIN kb.library l ON l.id=g.library_id
           WHERE l.organisation_id=%s
             AND l.name='one-concurrent-library'
             AND g.principal_id=%s
             AND g.role='manager'""",
            (world.organisation(), people.owner),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, LIBRARY_CREATE, key),
        )
        == 1
    )


def test_concurrent_project_creates_with_one_key_return_the_same_project(db_pool, world, people):
    principal = _actor(world, people.owner)
    key = str(uuid4())
    spec = _project_spec(
        world.organisation(),
        "one-concurrent-project",
        "the original description",
        key,
    )

    first, second = _race(
        db_pool,
        lambda conn: project_catalog.create_project(conn, principal, spec),
    )

    assert first.project.id == second.project.id
    assert first.description == second.description == "the original description"
    assert (
        world.count(
            "SELECT count(*) FROM kb.library "
            "WHERE organisation_id=%s AND name='one-concurrent-project'",
            (world.organisation(),),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.project p
               JOIN kb.library l ON l.id=p.id
               WHERE l.organisation_id=%s AND l.name='one-concurrent-project'""",
            (world.organisation(),),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
               WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, PROJECT_CREATE, key),
        )
        == 1
    )
    assert (
        world.count(
            "SELECT count(*) FROM kb.operation_receipt WHERE target_id=%s",
            (first.project.id,),
        )
        == 1
    )


def test_changed_project_description_with_an_existing_key_is_a_conflict(db_pool, world, people):
    principal = _actor(world, people.owner)
    key = str(uuid4())
    accepted = _create_project(
        db_pool,
        principal,
        world.organisation(),
        "project-description-conflict",
        "accepted description",
        key,
    )

    changed = _project_spec(
        world.organisation(),
        accepted.project.name,
        "changed description",
        key,
    )
    with db_pool.connection() as conn:
        with pytest.raises(ValueError, match="idempotency_conflict"):
            project_catalog.create_project(conn, principal, changed)

    assert accepted.description == "accepted description"
    assert (
        world.count(
            """SELECT count(*) FROM kb.project p
               JOIN kb.library l ON l.id=p.id
               WHERE l.organisation_id=%s AND l.name=%s""",
            (world.organisation(), accepted.project.name),
        )
        == 1
    )
    assert (
        world.count(
            "SELECT count(*) FROM kb.operation_receipt WHERE target_id=%s",
            (accepted.project.id,),
        )
        == 1
    )


def test_failed_project_receipt_rolls_back_all_rows_and_can_retry_the_key(
    db_pool, world, people, monkeypatch
):
    principal = _actor(world, people.owner)
    key = str(uuid4())

    def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("receipt insert failed")

    with monkeypatch.context() as patch:
        patch.setattr(project_catalog, "record_create_operation", fail_receipt)
        with pytest.raises(RuntimeError, match="receipt insert failed"):
            _create_project(
                db_pool,
                principal,
                world.organisation(),
                "atomic-project",
                "atomic description",
                key,
            )

    assert (
        world.count(
            """SELECT count(*) FROM kb.library
               WHERE organisation_id=%s AND name='atomic-project'""",
            (world.organisation(),),
        )
        == 0
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.library_grant g
               JOIN kb.library l ON l.id=g.library_id
               WHERE l.organisation_id=%s AND l.name='atomic-project'""",
            (world.organisation(),),
        )
        == 0
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
               WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, PROJECT_CREATE, key),
        )
        == 0
    )

    created = _create_project(
        db_pool,
        principal,
        world.organisation(),
        "atomic-project",
        "atomic description",
        key,
    )
    assert created.description == "atomic description"
    assert (
        world.count(
            "SELECT count(*) FROM kb.project WHERE id=%s",
            (created.project.id,),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
               WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, PROJECT_CREATE, key),
        )
        == 1
    )


def test_project_replay_denies_manager_permission_after_revocation(db_pool, world, people):
    principal = _actor(world, people.owner)
    key = str(uuid4())
    spec = _project_spec(
        world.organisation(),
        "revoked-project-replay",
        "still private",
        key,
    )
    with db_pool.connection() as conn:
        created = project_catalog.create_project(conn, principal, spec)

    world.conn.execute(
        "UPDATE kb.library_grant SET role='reader' WHERE library_id=%s AND principal_id=%s",
        (created.project.id, people.owner),
    )
    with db_pool.connection() as conn:
        with pytest.raises(AccessDenied):
            project_catalog.create_project(conn, principal, spec)

    world.conn.execute(
        "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
        (created.project.id, people.owner),
    )
    with db_pool.connection() as conn:
        with pytest.raises(AccessDenied):
            project_catalog.create_project(conn, principal, spec)

    assert (
        world.count(
            "SELECT count(*) FROM kb.project WHERE id=%s",
            (created.project.id,),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
               WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, PROJECT_CREATE, key),
        )
        == 1
    )


def test_concurrent_rule_replacement_allocates_one_new_version(db_pool, world, people):
    principal = _actor(world, people.owner)
    library = _create_library(db_pool, principal, world.organisation(), "rule-lineage-library")
    initial = _create_rule(db_pool, principal, library.id, _rule())

    key = str(uuid4())
    replacement = _rule(key=key, title="replacement", action="replace the thing")

    def replace(conn):
        return writes.create_rule(conn, principal, library.id, replacement, initial["id"])

    first, second = _race(db_pool, replace)

    assert first["id"] == second["id"]
    assert first["version_no"] == second["version_no"] == 2
    versions = [
        int(row[0])
        for row in world.conn.execute(
            "SELECT version_no FROM kb.rule WHERE lineage_id=%s ORDER BY version_no",
            (initial["lineage_id"],),
        ).fetchall()
    ]
    assert versions == [1, 2]
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, RULE_CREATE, key),
        )
        == 1
    )


def test_changed_payload_with_an_existing_key_is_a_conflict(db_pool, world, people):
    principal = _actor(world, people.owner)
    library_key = str(uuid4())
    library = _create_library(
        db_pool,
        principal,
        world.organisation(),
        "library-conflict",
        library_key,
    )

    changed_library = _library_spec(
        world.organisation(),
        "changed-after-uncertain-outcome",
        library_key,
    )
    with db_pool.connection() as conn:
        with pytest.raises(ValueError, match="idempotency_conflict"):
            library_catalog.create_library(conn, principal, changed_library)

    initial = _create_rule(
        db_pool,
        principal,
        library.id,
        _rule(title="original"),
    )
    rule_key = str(uuid4())
    accepted_body = _rule(key=rule_key, title="accepted replacement")
    accepted = _create_rule(
        db_pool,
        principal,
        library.id,
        accepted_body,
        initial["id"],
    )
    assert accepted["version_no"] == 2

    with db_pool.connection() as conn:
        with pytest.raises(ValueError, match="idempotency_conflict"):
            writes.create_rule(
                conn,
                principal,
                library.id,
                _rule(key=rule_key, title="changed replacement"),
                initial["id"],
            )

    assert (
        world.count(
            "SELECT count(*) FROM kb.library WHERE organisation_id=%s AND name IN (%s,%s)",
            (
                world.organisation(),
                "library-conflict",
                "changed-after-uncertain-outcome",
            ),
        )
        == 1
    )
    versions = [
        int(row[0])
        for row in world.conn.execute(
            "SELECT version_no FROM kb.rule WHERE lineage_id=%s ORDER BY version_no",
            (initial["lineage_id"],),
        ).fetchall()
    ]
    assert versions == [1, 2]


def test_same_key_is_isolated_between_principals(db_pool, world, people, run_sql):
    first_account = world.organisation()
    second_account = uuid4()
    world.conn.execute(
        "INSERT INTO kb.organisation(id,name) VALUES(%s,%s)",
        (second_account, f"idempotency-{second_account.hex[:8]}"),
    )
    first_actor = _actor(world, people.owner, first_account)
    second_actor = _actor(world, people.colleague, second_account)
    key = str(uuid4())

    first = _create_library(db_pool, first_actor, first_account, "same-key-first", key)
    second = _create_library(db_pool, second_actor, second_account, "same-key-second", key)

    assert first.id != second.id
    assert (
        world.count(
            "SELECT count(*) FROM kb.operation_receipt WHERE idempotency_key=%s",
            (key,),
        )
        == 2
    )

    receipt_query = """SELECT count(*) FROM kb.operation_receipt
                      WHERE idempotency_key=%s"""
    own = run_sql(
        receipt_query,
        role="kb_app",
        principal=people.owner,
        params=(key,),
    )
    colleague = run_sql(
        receipt_query,
        role="kb_app",
        principal=people.colleague,
        params=(key,),
    )
    other = run_sql(
        receipt_query,
        role="kb_app",
        principal=people.stranger,
        params=(key,),
    )
    assert own == (0, "1"), own
    assert colleague == (0, "1"), colleague
    assert other == (0, "0"), other


def test_replay_rechecks_library_manager_and_rule_contributor_rights(db_pool, world, people):
    principal = _actor(world, people.owner)
    library_key = str(uuid4())
    library = _create_library(
        db_pool,
        principal,
        world.organisation(),
        "revoked-replay-library",
        library_key,
    )
    spec = _library_spec(world.organisation(), "revoked-replay-library", library_key)
    world.conn.execute(
        "UPDATE kb.library_grant SET role='reader' WHERE library_id=%s AND principal_id=%s",
        (library.id, people.owner),
    )
    with db_pool.connection() as conn:
        with pytest.raises(AccessDenied):
            library_catalog.create_library(conn, principal, spec)

    world.conn.execute(
        "UPDATE kb.library_grant SET role='manager' WHERE library_id=%s AND principal_id=%s",
        (library.id, people.owner),
    )
    rule_key = str(uuid4())
    body = _rule(key=rule_key)
    first = _create_rule(db_pool, principal, library.id, body)
    world.conn.execute(
        "UPDATE kb.library_grant SET role='reader' WHERE library_id=%s AND principal_id=%s",
        (library.id, people.owner),
    )
    with db_pool.connection() as conn:
        with pytest.raises(AccessDenied):
            writes.create_rule(conn, principal, library.id, body)

    assert (
        world.count(
            "SELECT count(*) FROM kb.library WHERE id=%s",
            (library.id,),
        )
        == 1
    )
    assert (
        world.count(
            "SELECT count(*) FROM kb.rule WHERE id=%s",
            (first["id"],),
        )
        == 1
    )
    assert (
        world.count(
            "SELECT count(*) FROM kb.rule WHERE lineage_id=%s",
            (first["lineage_id"],),
        )
        == 1
    )


def test_failed_receipt_rolls_back_library_and_can_retry_the_key(
    db_pool, world, people, monkeypatch
):
    principal = _actor(world, people.owner)
    key = str(uuid4())

    def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("receipt insert failed")

    with monkeypatch.context() as patch:
        patch.setattr(library_catalog, "record_create_operation", fail_receipt)
        with pytest.raises(RuntimeError, match="receipt insert failed"):
            _create_library(
                db_pool,
                principal,
                world.organisation(),
                "rollback-library",
                key,
            )

    assert (
        world.count(
            "SELECT count(*) FROM kb.library WHERE organisation_id=%s AND name='rollback-library'",
            (world.organisation(),),
        )
        == 0
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, LIBRARY_CREATE, key),
        )
        == 0
    )

    created = _create_library(
        db_pool,
        principal,
        world.organisation(),
        "rollback-library",
        key,
    )
    assert (
        world.count(
            "SELECT count(*) FROM kb.library WHERE id=%s",
            (created.id,),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, LIBRARY_CREATE, key),
        )
        == 1
    )


def test_failed_receipt_rolls_back_rule_action_and_version_allocation(
    db_pool, world, people, monkeypatch
):
    principal = _actor(world, people.owner)
    library = _create_library(
        db_pool,
        principal,
        world.organisation(),
        "rollback-rule-library",
    )
    initial = _create_rule(
        db_pool,
        principal,
        library.id,
        _rule(action="original action"),
    )
    key = str(uuid4())
    rolled_back_action = f"must roll back {key}"
    body = _rule(
        key=key,
        title="rolled back replacement",
        action=rolled_back_action,
    )

    def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("receipt insert failed")

    with monkeypatch.context() as patch:
        patch.setattr(writes, "record_create_operation", fail_receipt)
        with pytest.raises(RuntimeError, match="receipt insert failed"):
            _create_rule(db_pool, principal, library.id, body, initial["id"])

    assert (
        world.count(
            "SELECT count(*) FROM kb.rule WHERE lineage_id=%s",
            (initial["lineage_id"],),
        )
        == 1
    )
    assert (
        world.count(
            "SELECT count(*) FROM kb.rule_action WHERE body=%s",
            (rolled_back_action,),
        )
        == 0
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, RULE_CREATE, key),
        )
        == 0
    )

    replacement = _create_rule(db_pool, principal, library.id, body, initial["id"])
    assert replacement["version_no"] == 2
    versions = [
        int(row[0])
        for row in world.conn.execute(
            "SELECT version_no FROM kb.rule WHERE lineage_id=%s ORDER BY version_no",
            (initial["lineage_id"],),
        ).fetchall()
    ]
    assert versions == [1, 2]
    assert (
        world.count(
            "SELECT count(*) FROM kb.rule_action WHERE rule_id=%s",
            (replacement["id"],),
        )
        == 1
    )
    assert (
        world.count(
            """SELECT count(*) FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
            (people.owner, RULE_CREATE, key),
        )
        == 1
    )


def test_keyless_creates_keep_the_legacy_non_idempotent_behaviour(db_pool, world, people):
    principal = _actor(world, people.owner)
    first = _create_library(
        db_pool,
        principal,
        world.organisation(),
        "keyless-one",
    )
    second = _create_library(
        db_pool,
        principal,
        world.organisation(),
        "keyless-two",
    )
    rule_one = _create_rule(db_pool, principal, first.id, _rule())
    rule_two = _create_rule(db_pool, principal, second.id, _rule())
    project_one = _create_project(db_pool, principal, world.organisation(), "keyless-project-one")
    project_two = _create_project(db_pool, principal, world.organisation(), "keyless-project-two")

    assert first.id != second.id
    assert project_one.project.id != project_two.project.id
    assert rule_one["id"] != rule_two["id"]
    assert rule_one["version_no"] == rule_two["version_no"] == 1
    assert (
        world.count(
            "SELECT count(*) FROM kb.operation_receipt WHERE principal_id=%s",
            (people.owner,),
        )
        == 0
    )


def test_receipt_schema_is_minimal_owner_isolated_and_immutable(db_pool, world, people, run_sql):
    principal = _actor(world, people.owner)
    key = str(uuid4())
    library = _create_library(
        db_pool,
        principal,
        world.organisation(),
        "receipt-security",
        key,
    )

    columns = run_sql(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema='kb' AND table_name='operation_receipt'"""
    )
    assert columns[0] == 0, columns
    assert set(columns[1].split()) == {
        "principal_id",
        "operation_type",
        "idempotency_key",
        "request_hash",
        "target_id",
        "created_at",
    }

    for statement in (
        "UPDATE kb.operation_receipt SET target_id=target_id",
        "DELETE FROM kb.operation_receipt",
        "TRUNCATE kb.operation_receipt",
    ):
        rc, output = run_sql(statement, role="kb_app", principal=people.owner)
        assert rc == 1, output
        assert "permission denied" in output.lower(), output

    for statement in (
        "UPDATE kb.operation_receipt SET target_id=target_id",
        "DELETE FROM kb.operation_receipt",
        "TRUNCATE kb.operation_receipt",
    ):
        with pytest.raises(
            psycopg.errors.ObjectNotInPrerequisiteState,
            match="operation receipts are immutable",
        ):
            world.conn.execute(statement)

    assert (
        world.count(
            "SELECT count(*) FROM kb.operation_receipt WHERE target_id=%s",
            (library.id,),
        )
        == 1
    )
