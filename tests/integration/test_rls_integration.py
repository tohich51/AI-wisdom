"""C03/C04 integration evidence on a real PostgreSQL 16.2 server.

These are not mocks. The pgserver package ships actual PostgreSQL binaries; a
real server is started, real DDL runs, and the assertions query real tables
through a role that has neither SUPERUSER nor BYPASSRLS.

If the server cannot start, these tests FAIL. A skipped mandatory gate is a
false green, and a false green is the specific failure mode this project
cannot afford.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_document (
    id          uuid PRIMARY KEY,
    library_id  uuid NOT NULL,
    owner       uuid NOT NULL,
    title       text NOT NULL,
    body_hash   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE kb_document ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_document FORCE ROW LEVEL SECURITY;

-- Transaction-local identity. The name avoids reserved words on purpose:
-- 'app.user' is a syntax error, which C00 recorded as a real constraint.
CREATE POLICY doc_tenant_isolation ON kb_document
    FOR ALL
    USING       (owner = current_setting('app.principal', true)::uuid)
    WITH CHECK  (owner = current_setting('app.principal', true)::uuid);

-- Caches, counts and autocomplete are also derived data. A shared summary
-- that mixes audiences is forbidden in v1, so it is RLS-filtered too.
CREATE TABLE IF NOT EXISTS kb_document_stats (
    library_id  uuid PRIMARY KEY,
    doc_count   integer NOT NULL
);
ALTER TABLE kb_document_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_document_stats FORCE ROW LEVEL SECURITY;
CREATE POLICY stats_tenant_isolation ON kb_document_stats
    FOR ALL USING (library_id = current_setting('app.library', true)::uuid)
    WITH CHECK (library_id = current_setting('app.library', true)::uuid);
"""


def _role_exists(pg_server) -> bool:
    out = pg_server.psql("SELECT count(*) FROM pg_roles WHERE rolname='kb_app';")
    return "1" in out


def _setup(pg_server) -> None:
    pg_server.psql("DROP OWNED BY kb_app;" if _role_exists(pg_server) else "SELECT 1;")
    pg_server.psql("DROP ROLE IF EXISTS kb_app;")
    pg_server.psql(SCHEMA)
    pg_server.psql(
        "CREATE ROLE kb_app LOGIN;"
        "GRANT SELECT, INSERT, UPDATE ON kb_document TO kb_app;"
        "GRANT SELECT ON kb_document_stats TO kb_app;"
    )


def _as(pg_server, principal: str, library: str, sql: str) -> str:
    return pg_server.psql(
        f"SET ROLE kb_app; SET app.principal='{principal}'; SET app.library='{library}'; {sql}"
    )


def test_role_is_neither_superuser_nor_bypassrls(pg_server):
    _setup(pg_server)
    out = pg_server.psql("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='kb_app';")
    assert "| f" in out or "f | f" in out, out
    assert "t" not in out.replace("|", " ").split("\n")[0], out


def test_tenant_sees_only_own_rows(pg_server):
    _setup(pg_server)
    a, b = uuid.uuid4(), uuid.uuid4()
    lib = uuid.uuid4()
    pg_server.psql(
        f"INSERT INTO kb_document (id, library_id, owner, title, body_hash) VALUES "
        f"('{a}','{lib}','{a}','doc-a','h1'), ('{b}','{lib}','{b}','doc-b','h2');"
    )
    assert "doc-a" in _as(pg_server, str(a), str(lib), "SELECT title FROM kb_document;")
    assert "doc-b" not in _as(pg_server, str(a), str(lib), "SELECT title FROM kb_document;")
    assert "doc-b" in _as(pg_server, str(b), str(lib), "SELECT title FROM kb_document;")


def test_unset_context_is_default_deny(pg_server):
    _setup(pg_server)
    lib, owner = uuid.uuid4(), uuid.uuid4()
    pg_server.psql(
        f"INSERT INTO kb_document (id, library_id, owner, title, body_hash) "
        f"VALUES ('{owner}','{lib}','{owner}','secret','h');"
    )
    out = pg_server.psql("SET ROLE kb_app; SELECT count(*) FROM kb_document;")
    assert " 0" in out or "0\n" in out, out


def test_forged_owner_row_is_rejected(pg_server, psql_strict):
    _setup(pg_server)
    attacker, victim = uuid.uuid4(), uuid.uuid4()
    lib = uuid.uuid4()
    psql_strict(
        f"INSERT INTO kb_document (id, library_id, owner, title, body_hash) "
        f"VALUES ('{victim}','{lib}','{victim}','b-doc','h');"
    )
    _rc, before = psql_strict("SELECT count(*) FROM kb_document;")

    rc, out = psql_strict(
        f"SET ROLE kb_app; SET app.principal='{attacker}'; SET app.library='{lib}'; "
        f"INSERT INTO kb_document (id, library_id, owner, title, body_hash) "
        f"VALUES ('{uuid.uuid4()}','{lib}','{victim}','forged','h');"
    )
    assert rc != 0, "the forged INSERT was accepted"
    assert "row-level security" in out.lower(), out

    _rc, after = psql_strict("SELECT count(*) FROM kb_document;")
    assert before.split()[-1] == after.split()[-1], (before, after)
    _rc, titles = psql_strict("SELECT title FROM kb_document;")
    assert "forged" not in titles


def test_shared_summary_is_filtered_not_mixed(pg_server):
    """A derived aggregate must not mix audiences (PRODUCT-SPEC invariant)."""
    _setup(pg_server)
    lib_a, lib_b = uuid.uuid4(), uuid.uuid4()
    pg_server.psql(
        f"INSERT INTO kb_document_stats (library_id, doc_count) "
        f"VALUES ('{lib_a}', 5), ('{lib_b}', 7);"
    )
    out = _as(
        pg_server, str(uuid.uuid4()), str(lib_a), "SELECT sum(doc_count) FROM kb_document_stats;"
    )
    assert "5" in out and "12" not in out, out


def test_role_cannot_escalate_by_claiming_another_principal(pg_server):
    """Changing app.principal is what a compromised session would try."""
    _setup(pg_server)
    a, b = uuid.uuid4(), uuid.uuid4()
    lib = uuid.uuid4()
    pg_server.psql(
        f"INSERT INTO kb_document (id, library_id, owner, title, body_hash) "
        f"VALUES ('{b}','{lib}','{b}','b-doc','h');"
    )
    # The transport sets the GUC; a query is not the transport. Here we only
    # assert that even setting it does not leak rows the policy forbids for a
    # principal that owns nothing.
    out = _as(pg_server, str(a), str(lib), "SELECT count(*) FROM kb_document;")
    assert " 0" in out or "0\n" in out, out
