"""Shared fixtures.

The PostgreSQL fixture starts a *real* PostgreSQL server (pgserver ships the
binaries). If it cannot start, integration tests fail rather than skip: a
skipped mandatory gate is a false green.

Environment note recorded during C03/C04 work: the PostgreSQL data directory
must NOT live on the workspace NAS mount. `initdb` fails there because the
mount does not provide the locking and permission semantics PostgreSQL
requires. The fixture therefore uses a local temporary directory.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "handoff" / "evidence"


@pytest.fixture(scope="session")
def pg_server():
    try:
        import pgserver
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"pgserver unavailable, cannot run a real PostgreSQL gate: {exc}")

    # local fs only — see module docstring
    pgdata = pathlib.Path(tempfile.mkdtemp(prefix="kb-pgdata-"))
    try:
        srv = pgserver.get_server(pgdata=str(pgdata), cleanup_mode=None)
        yield srv
    finally:
        # The server is still holding the directory; stopping it is the
        # fixture's job, and leaving it running across a session would leak
        # a postmaster.
        try:
            srv.cleanup()
        except Exception as exc:  # a surviving postmaster is worth knowing about
            print(f"warning: pg cleanup failed: {exc}")
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture
def psql_strict(pg_server):
    """Run SQL and capture stderr too.

    ``PostgresServer.psql`` returns stdout only and lets errors land on the
    console, so a policy rejection is invisible to an assertion. This helper
    sets ON_ERROR_STOP=1 and returns (returncode, combined output), which lets
    a test assert on the server's own rejection text.
    """
    import subprocess

    from pgserver.postgres_server import POSTGRES_BIN_PATH

    def _run(sql: str) -> tuple[int, str]:
        proc = subprocess.run(  # noqa: S603 - absolute path to the bundled psql
            [
                str(POSTGRES_BIN_PATH / "psql"),
                pg_server.get_uri(),
                "-v",
                "ON_ERROR_STOP=1",
                "--tuples-only",
            ],
            input=sql.encode(),
            capture_output=True,
            timeout=60,
        )
        return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")

    return _run


@pytest.fixture
def evidence_dir():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    return EVIDENCE
