"""Prepared plans must never cache permission decisions on a reused connection."""

from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import transaction_identity

pytestmark = pytest.mark.integration


def test_reused_prepared_connection_observes_policy_grant_and_membership_changes(world):
    actor = replace(world["principal"], cloud_client=True)
    with (
        psycopg.connect(world["admin"], autocommit=True) as admin,
        psycopg.connect(world["dsn"], autocommit=True, prepare_threshold=0) as conn,
    ):
        assert conn.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False, False)

        def role():
            with transaction_identity(conn, actor):
                return conn.execute(
                    "SELECT kb.effective_role(%s,%s)", (actor.principal_id, world["lib"])
                ).fetchone()[0]

        assert role() is None  # Missing cloud policy defaults closed.
        admin.execute(
            "INSERT INTO kb.library_policy(library_id,cloud_mcp_allowed) VALUES(%s,true)",
            (world["lib"],),
        )
        for _ in range(6):
            assert role() == "manager"
        admin.execute(
            "UPDATE kb.library_policy SET external_processing_allowed=false WHERE library_id=%s",
            (world["lib"],),
        )
        assert role() is None
        admin.execute(
            "UPDATE kb.library_policy SET external_processing_allowed=true WHERE library_id=%s",
            (world["lib"],),
        )
        assert role() == "manager"
        admin.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (world["lib"], actor.principal_id),
        )
        assert role() is None
        admin.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,'reader')",
            (world["lib"], actor.principal_id),
        )
        assert role() == "reader"
        admin.execute(
            "UPDATE kb.membership SET status='deactivated',"
            "deactivated_at=now(),deactivated_by=principal_id "
            "WHERE organisation_id=%s AND principal_id=%s",
            (actor.account_id, actor.principal_id),
        )
        assert role() is None
        admin.execute(
            "UPDATE kb.membership SET status='active' WHERE organisation_id=%s AND principal_id=%s",
            (actor.account_id, actor.principal_id),
        )
        assert role() == "reader"


def test_policy_defaults_null_inputs_and_inactive_membership_across_organisations(world):
    actor = world["principal"]
    with (
        psycopg.connect(world["admin"], autocommit=True) as admin,
        psycopg.connect(world["dsn"], autocommit=True, prepare_threshold=0) as conn,
    ):
        with transaction_identity(conn, actor):
            purposes = ["external", "cloud", "export", "generation", "unknown", None]
            for library, expected in [
                (world["lib"], [True, False, True, True, False, False]),
                (None, [True, False, True, False, False, False]),
            ]:
                assert [
                    conn.execute("SELECT kb.library_policy_allows(%s,%s)", (library, p)).fetchone()[
                        0
                    ]
                    for p in purposes
                ] == expected
            assert conn.execute("SELECT kb.membership_allows(NULL)").fetchone()[0] is True
            assert (
                conn.execute("SELECT kb.effective_role(NULL,%s)", (world["lib"],)).fetchone()[0]
                is None
            )
        other_org = uuid4()
        admin.execute("INSERT INTO kb.organisation(id,name) VALUES(%s,'Fixture')", (other_org,))
        admin.execute(
            "INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,status,"
            "deactivated_at,deactivated_by) "
            "VALUES(%s,%s,'https://other.identity.test',%s,'deactivated',now(),%s)",
            (other_org, actor.principal_id, str(uuid4()), actor.principal_id),
        )
        with transaction_identity(conn, actor):
            assert conn.execute(
                "SELECT kb.membership_allows(%s),kb.effective_role(%s,%s)",
                (actor.principal_id, actor.principal_id, world["lib"]),
            ).fetchone() == (False, None)
