"""Offline orchestration checks; real database behavior has a separate runtime test."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def bootstrap_module():
    path = Path(__file__).resolve().parents[2] / "scripts/bootstrap.py"
    spec = importlib.util.spec_from_file_location("portable_bootstrap", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(module, monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["bootstrap.py", *args])
    connect = MagicMock(side_effect=AssertionError("unexpected database access"))
    monkeypatch.setattr(module.psycopg, "connect", connect)
    # A DSN path must work even with no native PostgreSQL package installed.
    monkeypatch.setitem(sys.modules, "pgserver", None)
    return connect


IDENTITY = ["--owner-issuer", "https://idp.example.com", "--owner-subject", "synthetic"]


@pytest.mark.parametrize(
    "args",
    [
        ["--dsn-env", "BOOTSTRAP_TEST_DSN"],
        [
            "--dsn-env",
            "BOOTSTRAP_TEST_DSN",
            "--owner-issuer",
            "https://idp.example.com",
        ],
        ["--dsn-env", "BOOTSTRAP_TEST_DSN", *IDENTITY, "--owner-principal", "not-uuid"],
        ["--dsn-env", "BOOTSTRAP_TEST_DSN", "--pgdata", "unused", *IDENTITY],
        ["--dsn-env", "not-an-env", *IDENTITY],
        ["--owner-subject", "synthetic"],
    ],
)
def test_invalid_identity_or_route_does_no_database_work(bootstrap_module, monkeypatch, args):
    module = bootstrap_module
    connect = prepare(module, monkeypatch, args)
    migrate, queue = MagicMock(), MagicMock()
    monkeypatch.setattr(module, "apply_migrations", migrate)
    monkeypatch.setattr(module, "install_queue", queue)
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
    connect.assert_not_called()
    migrate.assert_not_called()
    queue.assert_not_called()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_environment_does_no_work(bootstrap_module, monkeypatch, value):
    module = bootstrap_module
    connect = prepare(module, monkeypatch, ["--dsn-env", "BOOTSTRAP_TEST_DSN", *IDENTITY])
    monkeypatch.delenv("BOOTSTRAP_TEST_DSN", raising=False)
    if value is not None:
        monkeypatch.setenv("BOOTSTRAP_TEST_DSN", value)
    migrate = MagicMock()
    monkeypatch.setattr(module, "apply_migrations", migrate)
    with pytest.raises(SystemExit):
        module.main()
    migrate.assert_not_called()
    connect.assert_not_called()


def test_dsn_orders_migrations_queue_owner_and_redacts(bootstrap_module, monkeypatch, capsys):
    module = bootstrap_module
    prepare(module, monkeypatch, ["--dsn-env", "BOOTSTRAP_TEST_DSN", *IDENTITY])
    sentinel = "postgresql://synthetic:DO_NOT_PRINT@invalid/db"
    monkeypatch.setenv("BOOTSTRAP_TEST_DSN", sentinel)
    calls = []

    def migrations(dsn, directory):
        assert dsn == sentinel and directory == module.MIGRATIONS
        calls.append("migrations")
        return ["0001_initial.sql"]

    def queue(dsn):
        assert dsn == sentinel
        calls.append("queue")

    def owner(srv, **kwargs):
        assert srv.get_uri() == sentinel
        assert kwargs["owner_subject"] == "synthetic"
        calls.append("owner")
        return dict.fromkeys(["libraries", "sources", "knowledge", "rules", "fragments"], 0)

    monkeypatch.setattr(module, "apply_migrations", migrations)
    monkeypatch.setattr(module, "install_queue", queue)
    monkeypatch.setattr(module, "bootstrap", owner)
    assert module.main() == 0
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err
    assert calls == ["migrations", "queue", "owner"]
    assert json.loads(captured.out)["empty_after_bootstrap"] is True


@pytest.mark.parametrize("stage", ["apply_migrations", "install_queue", "bootstrap"])
def test_cli_errors_hide_dsn(bootstrap_module, monkeypatch, capsys, stage):
    module = bootstrap_module
    prepare(module, monkeypatch, ["--dsn-env", "BOOTSTRAP_TEST_DSN", *IDENTITY])
    monkeypatch.setenv("BOOTSTRAP_TEST_DSN", "SECRET_DSN")
    monkeypatch.setattr(module, "apply_migrations", MagicMock(return_value=[]))
    monkeypatch.setattr(module, "install_queue", MagicMock())
    monkeypatch.setattr(module, stage, MagicMock(side_effect=RuntimeError("SECRET_DSN")))
    assert module.main() == 1
    captured = capsys.readouterr()
    assert "SECRET_DSN" not in captured.out + captured.err


def test_scalar_uses_psycopg_and_commits_context(bootstrap_module, monkeypatch):
    module = bootstrap_module
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = ("synthetic-value",)
    context = MagicMock()
    context.__enter__.return_value = conn
    monkeypatch.setattr(module.psycopg, "connect", MagicMock(return_value=context))
    result = module.scalar(SimpleNamespace(get_uri=lambda: "synthetic"), "SELECT 1")
    assert result == "synthetic-value"
    context.__exit__.assert_called_once_with(None, None, None)


def test_native_default_retained(bootstrap_module, monkeypatch, capsys):
    module = bootstrap_module
    monkeypatch.setattr(sys, "argv", ["bootstrap.py"])
    server = module.ExistingDatabase("synthetic")
    native = MagicMock(return_value=server)
    monkeypatch.setitem(sys.modules, "pgserver", SimpleNamespace(get_server=native))
    monkeypatch.setattr(module, "apply_migrations", MagicMock(return_value=[]))
    queue = MagicMock()
    monkeypatch.setattr(module, "install_queue", queue)
    monkeypatch.setattr(
        module,
        "bootstrap",
        MagicMock(
            return_value=dict.fromkeys(
                ["libraries", "sources", "knowledge", "rules", "fragments"], 0
            )
        ),
    )
    assert module.main() == 0
    native.assert_called_once_with(
        pgdata="/tmp/kb-bootstrap-pg",  # noqa: S108 - historic default contract
        cleanup_mode=None,
    )
    queue.assert_called_once_with("synthetic")
    capsys.readouterr()
