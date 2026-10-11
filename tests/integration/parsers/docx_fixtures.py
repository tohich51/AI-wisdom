"""C12A — shared fixtures for the DOCX parser suite.

Loaded into each test module by ``pytest_plugins = "docx_fixtures"`` at the top
of that module. A ``conftest.py`` would be the usual place for this, but the card
allows ``tests/integration/parsers/docx*`` and a file called ``conftest.py`` does
not match it, so the shared half is an ordinary module and the test modules opt
into it explicitly. Nothing here is a mock:

* The documents are real ``.docx`` packages under ``evals/fixtures/docx``,
  written by ``evals/fixtures/docx/build_fixtures.py`` with python-docx and
  committed as binaries. They are synthetic by construction — no real document
  is in this repository and none is downloaded.
* The database is a real PostgreSQL 16.2 started by ``pgserver`` from
  ``tests/conftest.py``, on a **dedicated database** ``kb_c12a``. Three other
  workers are on their own branches with their own suites, and
  ``CREATE POLICY``/``CREATE TRIGGER`` are not idempotent, so this suite applies
  the migrations exactly once to a database of its own rather than sharing one.
* The data directory and any scratch file is under ``tmp_path`` on local disk,
  never on the NAS-backed workspace.
"""

from __future__ import annotations

import pathlib
import uuid
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

# this file is tests/integration/parsers/docx_fixtures.py, so parents[3] is ROOT
ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
DOCX_FIXTURES = ROOT / "evals" / "fixtures" / "docx"
C12A_DB = "kb_c12a"

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def fixture_path(name: str) -> pathlib.Path:
    path = DOCX_FIXTURES / name
    if not path.exists():  # pragma: no cover - a missing fixture is a build error
        pytest.fail(f"fixture {name} is missing; run evals/fixtures/docx/build_fixtures.py")
    return path


@pytest.fixture(scope="session")
def docx_dsn(pg_server) -> str:
    """A dedicated database on the shared real server. Created, never reused."""
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C12A_DB,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C12A_DB)))
    return make_conninfo(base, dbname=C12A_DB)


@pytest.fixture(scope="session")
def docx_schema(docx_dsn: str) -> bool:
    """Apply every migration ONCE per session, structure only, no content.

    ``CREATE POLICY`` and ``CREATE TRIGGER`` abort on a second run, so the apply
    order is the sorted glob and it happens exactly once per database.
    """
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(docx_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture
def docx_owner(docx_schema, docx_dsn: str):
    """A direct owner connection for arranging rows.

    This is the migration role, not the runtime role. Assertions about what a
    *caller* may see still go through ``app.principal`` as ``kb_app``; a
    superuser BYPASSES RLS, so a pool connected as postgres would make every
    access assertion pass vacuously.
    """
    with psycopg.connect(docx_dsn, autocommit=True) as conn:
        yield conn


class World:
    """Arranges a source to hang fragments off. Never answers an access question."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._org: UUID | None = None

    def library(self) -> UUID:
        if self._org is None:
            self._org = uuid.uuid4()
            self.conn.execute(
                "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
                (self._org, f"c12a-org-{self._org.hex[:8]}"),
            )
        lib = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind, audience_scope) "
            "VALUES (%s, %s, %s, 'reference', 'private')",
            (lib, self._org, f"c12a-lib-{lib.hex[:8]}"),
        )
        return lib

    def source(self, library_id: UUID, payload: bytes) -> UUID:
        import hashlib

        digest = hashlib.sha256(payload).hexdigest()
        source_id = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.source (id, library_id, title, submitted_by, media_type, "
            "object_key, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                source_id,
                library_id,
                "synthetic brand document",
                uuid.uuid4(),
                DOCX_MEDIA_TYPE,
                f"blobs/{library_id.hex}/{digest[:2]}/{digest[2:4]}/{digest}.docx",
                digest,
            ),
        )
        return source_id

    def grant(self, library_id: UUID, principal_id: UUID, role: str = "reader") -> None:
        self.conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES (%s, %s, %s)",
            (library_id, principal_id, role),
        )

    def fragment_columns(self) -> dict[str, str]:
        return {
            name: data_type
            for name, data_type in self.conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'kb' AND table_name = 'fragment'"
            ).fetchall()
        }


@pytest.fixture
def world(docx_owner) -> World:
    return World(docx_owner)


@pytest.fixture
def kb_app(docx_dsn: str):
    """A real pool connected as ``kb_app`` with a real principal.

    Used only where RLS is the thing under test. ``options=-c role=kb_app``
    means the session cannot be a superuser by accident.
    """
    from psycopg_pool import ConnectionPool

    from kb.access.policy import clear_identity

    def _reset(conn: psycopg.Connection) -> None:
        conn.rollback()
        clear_identity(conn)
        conn.commit()

    pool = ConnectionPool(
        docx_dsn,
        min_size=1,
        max_size=3,
        open=True,
        reset=_reset,
        kwargs={"autocommit": False, "options": "-c role=kb_app"},
    )
    pool.wait(timeout=30)
    yield pool
    pool.close()
