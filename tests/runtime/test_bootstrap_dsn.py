"""Real PostgreSQL DSN bootstrap regression; run alongside test_clean_bootstrap.py."""

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pgserver
import psycopg
import pytest

pytestmark = [pytest.mark.integration]


def test_fresh_dsn_queue_owner_repeat_and_no_identity_writes(
    tmp_path, monkeypatch, capsys
):
    path = Path(__file__).resolve().parents[2] / "scripts/bootstrap.py"
    spec = importlib.util.spec_from_file_location("dsn_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # A fresh isolated native fixture supplies a real existing server connection.
    # The bootstrap itself must never call pgserver or a native executable.
    server = pgserver.get_server(pgdata=tmp_path / "database", cleanup_mode="delete")
    dsn = server.get_uri()
    monkeypatch.setenv("BOOTSTRAP_TEST_DSN", dsn)

    def forbidden(**kwargs):
        raise AssertionError("DSN bootstrap attempted native startup")

    monkeypatch.setattr(pgserver, "get_server", forbidden)
    base = ["bootstrap.py", "--dsn-env", "BOOTSTRAP_TEST_DSN"]
    monkeypatch.setattr(sys, "argv", base)
    with pytest.raises(SystemExit):
        module.main()
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('kb')").fetchone()[0] is None
        assert (
            conn.execute(
                "SELECT to_regclass('public.pkb_schema_migration')"
            ).fetchone()[0]
            is None
        )
    owner = str(uuid4())
    args = [
        *base,
        "--organisation",
        "Synthetic DSN regression",
        "--owner-principal",
        owner,
        "--owner-issuer",
        "https://idp.example.com",
        "--owner-subject",
        "synthetic",
    ]
    monkeypatch.setattr(sys, "argv", args)
    capsys.readouterr()
    assert module.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["owner_configured"] is True
    assert first["empty_after_bootstrap"] is True
    assert first["migrations_applied"]
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute("SELECT count(*) FROM public.pkb_queue_schema").fetchone()[0]
            == 1
        )
        assert conn.execute(
            "SELECT to_regclass('public.procrastinate_jobs')"
        ).fetchone()[0]
    assert module.main() == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["organisation_id"] == first["organisation_id"]
    assert repeated["migrations_applied"] == []
    assert repeated["owner_membership"] == "existing"
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT count(*) FROM kb.membership").fetchone()[0] == 1
        assert (
            conn.execute("SELECT count(*) FROM kb.organisation_admin").fetchone()[0]
            == 1
        )
        conn.execute(
            "UPDATE kb.membership SET status='deactivated', deactivated_at=now(),"
            " deactivated_by=%s WHERE principal_id=%s",
            (owner, owner),
        )
    assert module.main() == 1
    assert dsn not in capsys.readouterr().err
    with psycopg.connect(dsn) as conn:
        assert (
            conn.execute("SELECT status FROM kb.membership").fetchone()[0]
            == "deactivated"
        )
