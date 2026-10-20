"""C29 — the exact-email grant-recipient lookup, against a real PostgreSQL.

A non-admin library manager can already grant by principal id but cannot *name* a
colleague, because the membership rows a non-admin may read are their own only.
So this is deliberately a targeted address-existence oracle, for a caller who
already grants here: one whole address, this organisation, three fields back. No
roster, prefix match, count or paging. Every refusal is one flat 42501 raised by
PostgreSQL itself, and afterwards the manager still reads exactly their own row.
Addresses are synthetic ``.invalid``; no role, table, provider or person is made.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import ValidationError
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access import membership
from kb.access.policy import Principal
from kb.catalog import libraries

pytestmark = pytest.mark.integration

RECIPIENT = "recipient@example.invalid"
ISSUER = "https://identity.test"


def _address(
    conn: psycopg.Connection, org: UUID, principal_id: UUID, email: str, name: str
) -> None:
    """Give an existing membership row a lookup-able address."""
    conn.execute(
        "UPDATE kb.membership SET email=%s, display_name=%s "
        "WHERE organisation_id=%s AND principal_id=%s",
        (email, name, org, principal_id),
    )
    conn.commit()


def _member(conn: psycopg.Connection, org: UUID, status: str, email: str, name: str) -> UUID:
    """Add one more synthetic member to the organisation, in either status.

    ``deactivated_membership_is_complete`` is a real constraint and stays as it
    is, so a deactivation row carries both of its columns and an active row
    carries neither. The deactivating actor is this row's own synthetic
    principal id — no real person, and the column has no foreign key to break.
    """
    principal_id = uuid4()
    conn.execute(
        """INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,
             status,email,display_name,deactivated_at,deactivated_by)
           VALUES(%s,%s,%s,%s,%s,%s,%s,
                  CASE WHEN %s<>'deactivated' THEN NULL ELSE now() END,
                  CASE WHEN %s<>'deactivated' THEN NULL ELSE %s::uuid END)""",
        (
            org,
            principal_id,
            ISSUER,
            str(principal_id),
            status,
            email,
            name,
            status,
            status,
            principal_id,
        ),
    )
    conn.commit()
    return principal_id


def _grant(conn: psycopg.Connection, library_id: UUID, principal_id: UUID, role: str) -> None:
    """Write one grant row as the database owner — setup only."""
    conn.execute(
        "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,%s)",
        (library_id, principal_id, role),
    )
    conn.commit()


def test_manager_resolves_one_exact_address_and_nothing_else(world):
    """The picker can fill itself in, and answers only a whole address."""
    org = world["principal"].account_id
    owner = world["principal"].principal_id
    manager, lib = world["other"], world["lib"]
    with psycopg.connect(world["admin"]) as admin, psycopg.connect(world["dsn"]) as conn:
        _address(admin, org, owner, RECIPIENT, "Recipient Owner")
        _grant(admin, lib, manager.principal_id, "manager")
        before = libraries.list_grants(conn, manager, lib)
        assert libraries.resolve_grant_recipient(
            conn, manager, lib, libraries.ResolveGrantRecipient(email=RECIPIENT)
        )["recipient"] == {
            "principal_id": owner,
            "display_name": "Recipient Owner",
            "email": RECIPIENT,
        }
        # Case and surrounding whitespace fold; the match stays whole.
        assert (
            libraries.resolve_grant_recipient(
                conn,
                manager,
                lib,
                libraries.ResolveGrantRecipient(email="  ReCiPiEnT@Example.INVALID "),
            )["recipient"]["principal_id"]
            == owner
        )
        _member(admin, org, "active", RECIPIENT, "Recipient Twin")
        _member(admin, org, "deactivated", "retired@example.invalid", "Retired")
        # A prefix, an unknown address, an ambiguous one, an inactive-only one.
        for address in (
            RECIPIENT[: -len(".invalid")],
            "nobody@example.invalid",
            RECIPIENT,
            "retired@example.invalid",
        ):
            assert libraries.resolve_grant_recipient(
                conn, manager, lib, libraries.ResolveGrantRecipient(email=address)
            ) == {"recipient": None}, address
        assert libraries.list_grants(conn, manager, lib) == before
    # The bounds and the extra="forbid" are the API's, not the database's.
    with pytest.raises(ValidationError):
        libraries.ResolveGrantRecipient(email="ab")
    with pytest.raises(ValidationError):
        libraries.ResolveGrantRecipient(email=RECIPIENT, principal_id=owner)


@pytest.mark.parametrize("case", ["no_role", "reader", "wrong_account", "unknown_library"])
def test_every_refusal_is_the_same_42501(world, case):
    """No role, reader, wrong verified account, missing library: one flat answer."""
    manager, lib = world["other"], world["lib"]
    with psycopg.connect(world["admin"]) as admin:
        _address(
            admin, manager.account_id, world["principal"].principal_id, RECIPIENT, "Recipient Owner"
        )
        if case == "reader":
            _grant(admin, lib, manager.principal_id, "reader")
    spec = libraries.ResolveGrantRecipient(email=RECIPIENT)
    principal, library_id = manager, lib
    if case == "wrong_account":
        principal = Principal(manager.principal_id, uuid4())
    if case == "unknown_library":
        library_id = uuid4()
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as caught:
        with psycopg.connect(world["dsn"]) as conn:
            libraries.resolve_grant_recipient(conn, principal, library_id, spec)
    # The raised exception also prints PostgreSQL's CONTEXT lines, so compare
    # the primary message and the SQLSTATE — still exact equality, not a
    # substring match and not a catch-all on the exception type.
    assert caught.value.sqlstate == "42501"
    assert caught.value.diag.message_primary == "forbidden"


def test_acl_is_kb_app_only_and_the_roster_did_not_widen(world):
    """EXECUTE is narrow, and the manager still reads exactly their own row."""
    manager = world["other"]
    with psycopg.connect(world["admin"]) as admin:
        granted = set(
            admin.execute(
                """SELECT COALESCE(r.rolname,'public'), a.privilege_type
                     FROM pg_proc AS p
                     JOIN pg_namespace AS n ON n.oid = p.pronamespace
                     CROSS JOIN LATERAL aclexplode(p.proacl) AS a
                     LEFT JOIN pg_roles AS r ON r.oid = a.grantee
                    WHERE n.nspname='kb' AND p.proname='resolve_grant_recipient'"""
            ).fetchall()
        )
        # proacl, not acldefault(): a missing REVOKE shows up as PUBLIC here.
        assert ("kb_app", "EXECUTE") in granted
        assert ("public", "EXECUTE") not in granted
        assert not [g for g, _ in granted if g in ("kb_worker", "kb_provisioner")]
        # Defined once, SECURITY DEFINER and STABLE, owned by no application role.
        assert admin.execute(
            """SELECT count(*), bool_and(p.prosecdef), bool_and(p.provolatile='s'),
                      bool_and(o.rolname NOT IN ('kb_app','kb_worker','kb_provisioner'))
                 FROM pg_proc AS p
                 JOIN pg_namespace AS n ON n.oid = p.pronamespace
                 JOIN pg_roles AS o ON o.oid = p.proowner
                WHERE n.nspname='kb' AND p.proname='resolve_grant_recipient'"""
        ).fetchone() == (1, True, True, True)
    # The lookup opens no new read: through the ordinary catalogue call, this
    # non-admin manager still sees their single membership row and nobody else's.
    with psycopg.connect(world["dsn"]) as conn:
        assert [row.principal_id for row in membership.list_memberships(conn, manager, manager.account_id)] == [
            manager.principal_id
        ]
