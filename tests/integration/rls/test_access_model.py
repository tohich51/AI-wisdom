"""C06 — real SQL negative tests for the access model.

Every case here asserts on a *denial*. A test that only proves the happy path
is worthless for an access model: the interesting behaviour is what does not
happen. No mocks: a real PostgreSQL 16.2, real DDL, real grants.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# this file sits at tests/integration/rls/, so the project root is parents[3]
# parents[2] is tests/ — a path that has no migrations/ directory, and an
# empty glob there fails SILENTLY: no migrations applied, no roles created.
ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))

OWNER = uuid.UUID("00000000-0000-4000-8000-000000000001")
READER = uuid.UUID("00000000-0000-4000-8000-000000000002")
CONTRIB = uuid.UUID("00000000-0000-4000-8000-000000000003")
STRANGER = uuid.UUID("00000000-0000-4000-8000-000000000004")


@pytest.fixture(scope="session")
def schema(psql_strict):
    """Apply the migrations ONCE per session.

    Re-applying per test does not work and fails confusingly: CREATE POLICY
    and CREATE TRIGGER are not idempotent, so the second run aborts halfway
    and the error names a policy rather than the real cause. Schema is
    session state; only the rows are per-test.
    """
    rc, out = psql_strict("".join(m.read_text(encoding="utf-8") for m in MIGRATIONS))
    if rc != 0:
        raise RuntimeError("migration failed:\n" + out[-2000:])
    return True


def _seed(run) -> dict:
    lib = str(uuid.uuid4())
    org = str(uuid.uuid4())
    run(
        f"INSERT INTO kb.organisation (id, name) VALUES ('{org}','c06-org');"
        f"INSERT INTO kb.library (id, organisation_id, name, kind) "
        f"VALUES ('{lib}','{org}','c06-lib','reference');"
        f"INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES "
        f"('{lib}','{READER}','reader'),"
        f"('{lib}','{CONTRIB}','contributor'),"
        f"('{lib}','{OWNER}','manager');"
    )
    return {"library": lib, "org": org}


def _as(run, principal: str, sql: str) -> str:
    _rc, out = run(
        f"SET ROLE kb_app; SELECT set_config('app.principal','{principal}',false); {sql}"
    )
    return out


def test_runtime_role_is_not_privileged(pg_server, psql_strict, schema):
    _rc, out = psql_strict(
        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
        "WHERE rolname IN ('kb_app','kb_worker');"
    )
    assert "t" not in out.split("\n\n")[0].replace("|", " ").split()[1:3], out
    # and it must not own the tables, or FORCE RLS would not apply to it
    _rc, owners = psql_strict("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname='kb';")
    assert "kb_app" not in owners, owners
    assert "kb_worker" not in owners, owners


def test_no_identity_means_no_rows(pg_server, psql_strict, schema):
    s = _seed(psql_strict)
    _rc, _out = psql_strict(
        f"INSERT INTO kb.source (library_id, title, media_type, submitted_by, "
        f"object_key, content_hash) "
        f"VALUES ('{s['library']}','t','text/plain','{OWNER}','a/b','{'0' * 64}');"
    )
    _rc, out = psql_strict("SET ROLE kb_app; SELECT count(*) FROM kb.source;")
    assert "0" in out, out


def test_reader_sees_but_cannot_write(pg_server, psql_strict, schema):
    s = _seed(psql_strict)
    _rc, _out = psql_strict(
        f"INSERT INTO kb.source (library_id, title, media_type, submitted_by, "
        f"object_key, content_hash) "
        f"VALUES ('{s['library']}','t','text/plain','{OWNER}','k/l','{'1' * 64}');"
    )
    assert "t" in _as(psql_strict, str(READER), "SELECT title FROM kb.source;")

    out = _as(
        psql_strict,
        str(READER),
        f"INSERT INTO kb.source (library_id,title,media_type,submitted_by,"
        f"object_key,content_hash) VALUES "
        f"('{s['library']}','x','text/plain','{READER}','r/x','{'2' * 64}');",
    )
    assert "row-level security" in out.lower(), out


def test_contributor_writes_but_cannot_grant(pg_server, psql_strict, schema):
    s = _seed(psql_strict)
    out = _as(
        psql_strict,
        str(CONTRIB),
        f"INSERT INTO kb.source (library_id,title,media_type,submitted_by,"
        f"object_key,content_hash) VALUES "
        f"('{s['library']}','ok','text/plain','{CONTRIB}','c/x','{'3' * 64}');",
    )
    assert "row-level security" not in out.lower(), out

    # contributor must not be able to grant themselves manager
    out = _as(
        psql_strict,
        str(CONTRIB),
        f"INSERT INTO kb.library_grant (library_id, principal_id, role) "
        f"VALUES ('{s['library']}','{CONTRIB}','manager');",
    )
    assert "row-level security" in out.lower(), out


def test_stranger_sees_nothing(pg_server, psql_strict, schema):
    s = _seed(psql_strict)
    _rc, _out = psql_strict(
        f"INSERT INTO kb.source (library_id,title,media_type,submitted_by,"
        f"object_key,content_hash) "
        f"VALUES ('{s['library']}','secret','text/plain','{OWNER}','s/x','{'4' * 64}');"
    )
    out = _as(psql_strict, str(STRANGER), "SELECT count(*) FROM kb.source;")
    assert "0" in out, out


def test_project_membership_does_not_grant_source_library(pg_server, psql_strict, schema):
    """A member of a project library gets nothing in the libraries it links."""
    s = _seed(psql_strict)
    proj = str(uuid.uuid4())
    _rc, _out = psql_strict(
        f"INSERT INTO kb.library (id, organisation_id, name, kind) "
        f"VALUES ('{proj}','{s['org']}','proj','project');"
        f"INSERT INTO kb.library_grant (library_id, principal_id, role) "
        f"VALUES ('{proj}','{READER}','curator');"
    )
    # reader is a curator of the project...
    assert "proj" in _as(
        psql_strict, str(READER), f"SELECT name FROM kb.library WHERE id='{proj}';"
    )
    # ...and still only a reader of the source library.
    #
    # Note the shape of the denial. An UPDATE under RLS is FILTERED, not
    # rejected: it reports "UPDATE 0" and raises nothing. INSERT is the one
    # that is rejected outright, via WITH CHECK. Application code must
    # therefore never read a zero row count as success.
    _rc, _seedout = psql_strict(
        f"INSERT INTO kb.source (library_id,title,media_type,submitted_by,"
        f"object_key,content_hash) VALUES "
        f"('{s['library']}','brand-book','text/plain','{OWNER}','p/x','{'5' * 64}');"
    )
    out = _as(
        psql_strict, str(READER), "UPDATE kb.source SET title='hijacked' WHERE title='brand-book';"
    )
    assert "UPDATE 0" in out, out
    assert "hijacked" not in _as(psql_strict, str(OWNER), "SELECT title FROM kb.source;")


def test_published_rule_is_immutable(pg_server, psql_strict, schema):
    s = _seed(psql_strict)
    _rc, _out = psql_strict(
        f"INSERT INTO kb.rule (id, library_id, version_no, title, when_to_apply, "
        f"expected_effect, publication) "
        f"VALUES (gen_random_uuid(),'{s['library']}',1,'r','when','effect','published');"
    )
    out = _as(psql_strict, str(OWNER), "UPDATE kb.rule SET title='changed';")
    assert "immutable" in out.lower(), out


def test_own_use_history_only(pg_server, psql_strict, schema):
    s = _seed(psql_strict)
    rule = str(uuid.uuid4())
    _rc, _out = psql_strict(
        f"INSERT INTO kb.rule (id, library_id, version_no, title) "
        f"VALUES ('{rule}','{s['library']}',1,'r');"
    )
    _rc, _out = psql_strict(
        f"INSERT INTO kb.use_record (id, rule_id, rule_version_no, principal_id) "
        f"VALUES (gen_random_uuid(),'{rule}',1,'{READER}'),"
        f"       (gen_random_uuid(),'{rule}',1,'{CONTRIB}');"
    )
    mine = _as(psql_strict, str(READER), "SELECT count(*) FROM kb.use_record;")
    assert "1" in mine, mine


def test_malformed_principal_is_not_an_identity(pg_server, psql_strict, schema):
    _seed(psql_strict)  # a library exists; the point is the bad principal, not the data
    _rc, out = psql_strict(
        "SET ROLE kb_app; "
        "SELECT set_config('app.principal','not-a-uuid',false); "
        "SELECT count(*) FROM kb.source;"
    )
    assert "0" in out, out
