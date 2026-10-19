"""C16 semantic context and bounded discovery tests on synthetic real PostgreSQL."""

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.jobs import indexer
from kb.migrate import apply_migrations
from kb.retrieval import semantic_registry as registry
from kb.retrieval.embedding_profile import SELECTED_PROFILE

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _operator(world):
    return make_conninfo(world["admin"], user="kb_provisioner")


def _explicit_context(conn, library_id, *, limit=1, rotation=0):
    return conn.execute(
        "SELECT library_id,principal_id,account_id "
        "FROM kb.semantic_index_context(%s::text,%s::uuid,%s,%s)",
        (SELECTED_PROFILE.profile_hash, library_id, limit, rotation),
    ).fetchall()


def _batch_context(conn, *, limit=1000, rotation=0):
    return conn.execute(
        "SELECT library_id,principal_id,account_id "
        "FROM kb.semantic_index_context(%s::text,NULL,%s,%s)",
        (SELECTED_PROFILE.profile_hash, limit, rotation),
    ).fetchall()


def _manager_scenario(world, lower, upper, foreign):
    organisation = uuid4()
    foreign_organisation = uuid4()
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "INSERT INTO kb.organisation(id,name) VALUES(%s,%s),(%s,%s)",
            (
                organisation,
                "c16-org-" + organisation.hex,
                foreign_organisation,
                "c16-foreign-org-" + foreign_organisation.hex,
            ),
        )
        for principal in (lower, upper):
            conn.execute(
                "INSERT INTO kb.membership("
                "organisation_id,principal_id,issuer,subject,status) "
                "VALUES(%s,%s,'https://c16.invalid',%s,'active')",
                (organisation, principal, str(principal)),
            )
        conn.execute(
            "INSERT INTO kb.membership("
            "organisation_id,principal_id,issuer,subject,status) "
            "VALUES(%s,%s,'https://c16.invalid',%s,'active')",
            (foreign_organisation, foreign, str(foreign)),
        )
        library = uuid4()
        conn.execute(
            "INSERT INTO kb.library(id,organisation_id,name,kind,audience_scope) "
            "VALUES(%s,%s,%s,'reference','private')",
            (library, organisation, "c16-context-" + library.hex),
        )
        for principal in (lower, upper, foreign):
            conn.execute(
                "INSERT INTO kb.library_grant(library_id,principal_id,role) "
                "VALUES(%s,%s,'manager')",
                (library, principal),
            )
        conn.execute(
            "INSERT INTO kb.knowledge(library_id,statement,kind,publication) "
            "VALUES(%s,'Published semantic candidate','assertion','published')",
            (library,),
        )
    return organisation, foreign_organisation, library


def test_context_is_tie_ordered_exact_organisation_and_current(world):
    lower = UUID(int=0x2001)
    upper = UUID(int=0x2002)
    foreign = UUID(int=0x1000)
    organisation, _foreign_organisation, library = _manager_scenario(world, lower, upper, foreign)

    with psycopg.connect(_operator(world), autocommit=True) as conn:
        assert _explicit_context(conn, library) == [(library, lower, organisation)]
        resolved = registry.resolve_context(conn, library)
        assert resolved is not None
        assert (resolved.principal_id, resolved.account_id) == (lower, organisation)
        assert library in registry.pending_libraries(conn, rotation=0)

    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "UPDATE kb.membership SET status='deactivated', "
            "deactivated_at=now(), deactivated_by=principal_id "
            "WHERE organisation_id=%s AND principal_id=%s",
            (organisation, lower),
        )
    with psycopg.connect(_operator(world), autocommit=True) as conn:
        assert _explicit_context(conn, library) == [(library, upper, organisation)]

    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "UPDATE kb.library_grant SET role='contributor' "
            "WHERE library_id=%s AND principal_id=%s",
            (library, upper),
        )
    with psycopg.connect(_operator(world), autocommit=True) as conn:
        assert _explicit_context(conn, library) == []
        assert registry.resolve_context(conn, library) is None
        assert library not in registry.pending_libraries(conn, rotation=0)

    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "UPDATE kb.membership SET status='active', "
            "deactivated_at=NULL, deactivated_by=NULL "
            "WHERE organisation_id=%s AND principal_id=%s",
            (organisation, lower),
        )
        conn.execute(
            "INSERT INTO kb.library_policy("
            "library_id,external_processing_allowed,cloud_mcp_allowed,export_allowed) "
            "VALUES(%s,false,true,true) ON CONFLICT(library_id) DO UPDATE "
            "SET external_processing_allowed=false",
            (library,),
        )
    with psycopg.connect(_operator(world), autocommit=True) as conn:
        assert _explicit_context(conn, library) == []
        assert library not in registry.pending_libraries(conn, rotation=0)


def test_task_resolves_a_fresh_replacement_on_every_attempt(world, tmp_path, monkeypatch):
    stale = UUID(int=0x3001)
    replacement = UUID(int=0x3002)
    foreign = UUID(int=0x2000)
    organisation, _foreign_organisation, library = _manager_scenario(
        world, stale, replacement, foreign
    )
    dsn = _operator(world)
    config = {"url": "http://synthetic.invalid", "keys_root": tmp_path}
    app, _enqueue = indexer.build_queue(dsn, config)
    task = app.tasks["kb.reindex_library"]
    invoke = task.func
    observations = []

    def observe_reindex(
        observed_dsn,
        principal,
        library_id,
        *,
        terminal_failure=True,
        **observed_config,
    ):
        observations.append(
            {
                "dsn": observed_dsn,
                "principal_id": principal.principal_id,
                "account_id": principal.account_id,
                "library_id": library_id,
                "terminal_failure": terminal_failure,
                "config": observed_config,
            }
        )
        return {"state": "observed"}

    monkeypatch.setattr(indexer, "reindex", observe_reindex)
    first = invoke(SimpleNamespace(job=SimpleNamespace(attempts=0)), str(library))
    assert first == {"state": "observed"}
    assert observations[-1]["principal_id"] == stale
    assert observations[-1]["account_id"] == organisation

    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "UPDATE kb.library_grant SET role='contributor' "
            "WHERE library_id=%s AND principal_id=%s",
            (library, stale),
        )
    retry = invoke(SimpleNamespace(job=SimpleNamespace(attempts=1)), str(library))
    assert retry == {"state": "observed"}
    assert observations[-1]["principal_id"] == replacement
    assert observations[-1]["account_id"] == organisation
    assert observations[-1]["library_id"] == library
    assert observations[-1]["terminal_failure"] is False
    assert observations[-1]["config"] == config

    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "UPDATE kb.membership SET status='deactivated', "
            "deactivated_at=now(), deactivated_by=principal_id "
            "WHERE organisation_id=%s AND principal_id=%s",
            (organisation, replacement),
        )
    terminal = invoke(SimpleNamespace(job=SimpleNamespace(attempts=2)), str(library))
    assert isinstance(terminal, dict)
    assert terminal.get("state") == "skipped"
    assert "semantic_context_unavailable" in terminal.values()
    assert len(observations) == 2
    assert all(row["account_id"] == organisation for row in observations)
    assert all(row["library_id"] == library for row in observations)


@pytest.mark.parametrize(
    "profile",
    [None, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64],
)
def test_profile_must_be_lowerhex64(world, profile):
    with pytest.raises(psycopg.errors.InvalidParameterValue) as raised:
        with psycopg.connect(_operator(world), autocommit=True) as conn:
            conn.execute(
                "SELECT * FROM kb.semantic_index_context(%s::text,NULL,1,0)",
                (profile,),
            )
    assert raised.value.sqlstate == "22023"


@pytest.mark.parametrize("limit", [None, 0, -1, 1001])
def test_batch_limit_must_be_between_one_and_one_thousand(world, limit):
    with pytest.raises(psycopg.errors.InvalidParameterValue) as raised:
        with psycopg.connect(_operator(world), autocommit=True) as conn:
            conn.execute(
                "SELECT * FROM kb.semantic_index_context(%s::text,NULL,%s,0)",
                (SELECTED_PROFILE.profile_hash, limit),
            )
    assert raised.value.sqlstate == "22023"


def test_explicit_library_requires_limit_one(world):
    with pytest.raises(psycopg.errors.InvalidParameterValue) as raised:
        with psycopg.connect(_operator(world), autocommit=True) as conn:
            _explicit_context(conn, uuid4(), limit=2)
    assert raised.value.sqlstate == "22023"


@pytest.mark.parametrize("role", ["kb_app", "kb_worker"])
def test_runtime_roles_cannot_execute_context_helper(world, role):
    dsn = make_conninfo(world["admin"], user=role)
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _batch_context(conn, limit=1)
    assert raised.value.sqlstate == "42501"


@pytest.fixture(scope="module")
def rotation_db(pg_server):
    name = "c16_rotation_" + uuid4().hex[:12]
    server_dsn = pg_server.get_uri()
    with psycopg.connect(server_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(server_dsn, dbname=name)
    try:
        assert apply_migrations(dsn, ROOT / "migrations")
        yield dsn
    finally:
        with psycopg.connect(server_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_rotation_window_wraps_and_covers_more_than_one_thousand_fixed_candidates(rotation_db):
    provisioner_dsn = make_conninfo(rotation_db, user="kb_provisioner")
    with psycopg.connect(provisioner_dsn, autocommit=True) as conn:
        assert _batch_context(conn, limit=1000, rotation=0) == []

    organisation = uuid4()
    manager = uuid4()
    with psycopg.connect(rotation_db) as conn:
        conn.execute(
            "INSERT INTO kb.organisation(id,name) VALUES(%s,%s)",
            (organisation, "c16-rotation-" + organisation.hex),
        )
        conn.execute(
            "INSERT INTO kb.membership("
            "organisation_id,principal_id,issuer,subject,status) "
            "VALUES(%s,%s,'https://c16.invalid',%s,'active')",
            (organisation, manager, str(manager)),
        )
        conn.execute(
            "INSERT INTO kb.library(id,organisation_id,name,kind,audience_scope) "
            "SELECT md5('c16-fixed-library:' || n::text)::uuid,%s,"
            "'c16-fixed-' || n::text,'reference','private' "
            "FROM generate_series(1,1005) AS series(n)",
            (organisation,),
        )
        conn.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) "
            "SELECT id,%s,'manager' FROM kb.library WHERE organisation_id=%s",
            (manager, organisation),
        )
        conn.execute(
            "INSERT INTO kb.knowledge(library_id,statement,kind,publication) "
            "SELECT id,'rotation candidate ' || id::text,'assertion','published' "
            "FROM kb.library WHERE organisation_id=%s",
            (organisation,),
        )
        ordered = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM kb.library WHERE organisation_id=%s ORDER BY id",
                (organisation,),
            ).fetchall()
        ]
    assert len(ordered) == 1005

    limit = 700
    with psycopg.connect(provisioner_dsn, autocommit=True) as conn:
        first = _batch_context(conn, limit=limit, rotation=0)
        second = _batch_context(conn, limit=limit, rotation=1)
    assert [row[0] for row in first] == ordered[:limit]
    offset = limit
    assert [row[0] for row in second] == (
        ordered[offset:] + ordered[: offset + limit - len(ordered)]
    )
    assert all(row == (row[0], manager, organisation) for row in first + second)
    assert set(row[0] for row in first) | set(row[0] for row in second) == set(ordered)
