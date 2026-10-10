"""C12B — integration fixtures: a real PostgreSQL 16.2, a real socket, real files.

Three things here are worth explaining, because each was forced by something real.

**A dedicated database, and the schema applied once.** ``pg_server`` from
``tests/conftest.py`` is session-scoped and shared with C10's suite, but the
migrations are not idempotent — ``CREATE POLICY`` is not — so this suite creates
its own database and applies ``migrations/0*.sql`` once per session. One server,
two schemas of state, no ordering dependency.

**A real loopback HTTP server.** ``page_server`` is a ``ThreadingHTTPServer``
bound to 127.0.0.1 on an ephemeral port. It counts every request it receives,
which is what makes "refused" a fact rather than an assertion: a refusal that
happened *after* the request would look identical from the caller's side. It
also serves the HTML fixtures, so the extraction is exercised over bytes that
crossed a real socket.

**One piece of pending DDL, standing in for a decision this card may not make.**
``kb.fragment`` has RLS enabled and forced with a SELECT policy and no write
policy, so ``kb_app`` cannot insert a fragment at all — found by running the
insert, not by reading the migration. The fix belongs to the single owner of
``migrations/``; this card cannot add one. ``pending_fragment_write_policy``
applies the proposed policy so the rest of the suite can run, in a fixture whose
name says what it is, and two tests in ``test_catalogue_record.py`` assert both
that the policy is genuinely absent from the repository and that without it the
insert fails loudly rather than silently reporting success.

The identity helper below is a stand-in for Keycloak and is not authentication:
a real deployment verifies a signed token at that point. What is under test here
does not depend on it being real, because no function in this card reads
identity from anything but the :class:`Principal` it is handed.
"""

from __future__ import annotations

import http.server
import pathlib
import threading
import uuid
from collections.abc import Iterator
from typing import ClassVar
from uuid import UUID

import psycopg
import pytest
from harness import (  # noqa: F401 - re-exported for the test modules
    FROZEN_NOW,
    PENDING_FRAGMENT_WRITE_POLICY,
    PUBLIC,
)
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from kb.access.policy import Principal
from kb.catalog.storage import LocalBlobStore

pytestmark = pytest.mark.integration


def pytest_collection_modifyitems(items) -> None:
    """Mark the tests in this directory as integration tests.

    Declared in one place rather than repeated in four files: these are the
    acceptance evidence for the card and they belong under ``just integration``.

    The path filter is not decoration. ``pytest_collection_modifyitems`` is a
    *global* hook — it is handed every item in the session, not only this
    directory's — so a filter-free loop would mark the whole repository's tests
    and ``just check`` would find nothing left to run.
    """
    here = pathlib.Path(__file__).resolve().parent
    for item in items:
        if pathlib.Path(str(item.path)).parent == here:
            item.add_marker(pytest.mark.integration)


# this file is tests/integration/fetch/conftest.py, so parents[3] is ROOT
ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
FIXTURES = ROOT / "evals" / "fixtures" / "html"
C12B_DB = "kb_c12b"

# PUBLIC (a routable address that is never dialled) and FROZEN_NOW (the retrieval
# instant every helper stamps) live in harness.py, next to the helpers using them.


# ------------------------------------------------------------------ database


@pytest.fixture(scope="session")
def c12b_dsn(pg_server) -> str:
    """A dedicated database on the shared real server. Created, never reused."""
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C12B_DB,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C12B_DB)))
    return make_conninfo(base, dbname=C12B_DB)


@pytest.fixture(scope="session")
def schema(c12b_dsn: str) -> bool:
    """Apply every migration ONCE per session. Structure only, no content.

    0004_uploads.sql supplies the ``source_version_write`` policy C10 found
    missing, which is why a version row can be written at all in this suite.
    """
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c12b_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture(scope="session")
def pending_fragment_write_policy(schema, c12b_dsn: str) -> bool:
    """Apply the proposed ``kb.fragment`` INSERT policy so the suite can run.

    Named for what it is. ``test_catalogue_record.py`` asserts that this policy
    is *not* in the repository, and that without it the insert is refused rather
    than silently accepted, so the stand-in cannot quietly become the answer.
    """
    with psycopg.connect(c12b_dsn, autocommit=True) as conn:
        conn.execute(PENDING_FRAGMENT_WRITE_POLICY)
    return True


@pytest.fixture(scope="session")
def admin(schema, c12b_dsn: str):
    """A direct owner connection for arranging fixtures.

    This is the migration role, not the runtime role. Seeding as an owner is how
    a library nobody can read gets created at all, because a library's creator
    always becomes its manager. Every assertion about what a *caller* may see
    still goes through ``app.principal`` as ``kb_app``.
    """
    with psycopg.connect(c12b_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def db_pool(schema, pending_fragment_write_policy, c12b_dsn: str):
    """A real connection pool for the runtime role, ``kb_app``.

    Never the migration superuser: a superuser BYPASSES RLS, so a pool connected
    as postgres would make every access assertion in this suite pass vacuously.
    """
    from kb.access.policy import clear_identity

    def _reset(conn: psycopg.Connection) -> None:
        conn.rollback()
        clear_identity(conn)
        conn.commit()

    pool = ConnectionPool(
        c12b_dsn,
        min_size=1,
        max_size=4,
        open=True,
        reset=_reset,
        kwargs={"autocommit": False, "options": "-c role=kb_app"},
    )
    pool.wait(timeout=30)
    yield pool
    pool.close()


@pytest.fixture
def runtime_conn(db_pool):
    """One real ``kb_app`` connection with a clean identity.

    ``clear_identity`` runs first: a connection that kept a principal answers
    the next caller's request as the previous caller, which would make an access
    assertion in one test depend on the test before it.
    """
    from kb.access.policy import clear_identity

    with db_pool.connection() as conn:
        conn.rollback()
        clear_identity(conn)
        conn.commit()
        yield conn


# ------------------------------------------------------------------- world


class World:
    """Arranges a scenario as the migration role. Never answers an access question."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._org: uuid.UUID | None = None

    def organisation(self) -> uuid.UUID:
        if self._org is None:
            self._org = uuid.uuid4()
            self.conn.execute(
                "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
                (self._org, f"c12b-org-{self._org.hex[:8]}"),
            )
        return self._org

    def library(self, *, name: str, kind: str = "reference") -> uuid.UUID:
        lib = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind) VALUES (%s, %s, %s, %s)",
            (lib, self.organisation(), f"{name}-{lib.hex[:8]}", kind),
        )
        return lib

    def grant(self, library_id: uuid.UUID, principal_id: uuid.UUID, role: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) "
            "VALUES (%s, %s, %s) ON CONFLICT (library_id, principal_id) "
            "DO UPDATE SET role = EXCLUDED.role",
            (library_id, principal_id, role),
        )

    def open_library(self, *, name: str, who: uuid.UUID, role: str = "contributor") -> uuid.UUID:
        lib = self.library(name=name)
        self.grant(lib, who, role)
        return lib

    def closed_library(self, *, name: str) -> uuid.UUID:
        return self.library(name=name)

    def revoke(self, library_id: uuid.UUID, principal_id: uuid.UUID) -> None:
        self.conn.execute(
            "DELETE FROM kb.library_grant WHERE library_id = %s AND principal_id = %s",
            (library_id, principal_id),
        )

    # Every count below is scoped to a library a test created. The `admin`
    # connection is session-scoped and the database is shared by the whole
    # suite, so an unscoped count would assert about other tests' rows too.

    def count(self, statement: str, params: tuple = ()) -> int:
        row = self.conn.execute(statement, params).fetchone()
        return int(row[0]) if row else 0

    def grants(self, library_id: uuid.UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT principal_id, role FROM kb.library_grant WHERE library_id = %s "
                "ORDER BY principal_id",
                (library_id,),
            ).fetchall()
        )

    def sources_in(self, library_id: uuid.UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT id, media_type, object_key, content_hash, processing, submitted_by "
                "FROM kb.source WHERE library_id = %s ORDER BY created_at",
                (library_id,),
            ).fetchall()
        )

    def fragments_of(self, source_id: uuid.UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT ordinal, locator_kind, paragraph, chapter, snapshot_url, snapshot_at, text "
                "FROM kb.fragment WHERE source_id = %s ORDER BY ordinal",
                (source_id,),
            ).fetchall()
        )

    def versions_of(self, source_id: uuid.UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT version_no, content_hash FROM kb.source_version "
                "WHERE source_id = %s ORDER BY version_no",
                (source_id,),
            ).fetchall()
        )

    def ingests_in(self, library_id: uuid.UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT state, content_hash, source_url, retrieved_at, submitted_by "
                "FROM kb.ingest WHERE library_id = %s ORDER BY created_at",
                (library_id,),
            ).fetchall()
        )

    def manifests_in(self, library_id: uuid.UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT m.object_key, m.content_hash, m.byte_size, m.media_type "
                "FROM kb.object_manifest m JOIN kb.source s ON s.id = m.source_id "
                "WHERE s.library_id = %s",
                (library_id,),
            ).fetchall()
        )


@pytest.fixture
def world(admin) -> World:
    return World(admin)


class People:
    """One test's cast, minted fresh so no test can observe another's rows."""

    def __init__(self) -> None:
        self.owner = uuid.uuid4()
        self.contributor = uuid.uuid4()
        self.reader = uuid.uuid4()
        self.stranger = uuid.uuid4()
        self.account = uuid.uuid4()

    def principal(self, who: UUID, *, watermark: int = 1) -> Principal:
        return Principal(principal_id=who, account_id=self.account, generation_watermark=watermark)


@pytest.fixture
def people() -> People:
    return People()


@pytest.fixture
def key() -> str:
    """A fresh idempotency key. The database requires at least eight characters."""
    return f"idem-{uuid.uuid4().hex}"


# ------------------------------------------------------------- object store


@pytest.fixture
def store(tmp_path) -> LocalBlobStore:
    """A real blob store on a local temp directory.

    ``tmp_path`` is local, never the NAS-backed workspace, so a fetched page
    never ends up in a backup of the repository.
    """
    return LocalBlobStore(tmp_path / "objects")


# ------------------------------------------------------------ a real server


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the HTML fixtures and counts every request it is given.

    The counter is the point: "the gateway refused" and "the gateway made the
    request and then refused the answer" look the same from outside, and only
    the server knows which happened.
    """

    hits: ClassVar[list[str]] = []
    routes: ClassVar[dict[str, tuple[int, str, bytes]]] = {}

    def do_GET(self) -> None:
        type(self).hits.append(self.path)
        route = type(self).routes.get(self.path)
        if route is None:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        status, content_type, body = route
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def page_server() -> Iterator[str]:
    """A real HTTP server on a real loopback socket, on an ephemeral port.

    The internal service A14 describes: reachable from the gateway's own host,
    and not reachable from the gateway's fetcher. Started once per test with a
    fresh route table and a fresh counter.
    """
    _Handler.hits = []
    _Handler.routes = {
        "/internal/secret": (200, "text/plain; charset=utf-8", b"an internal service"),
        "/article": (200, "text/html; charset=utf-8", (FIXTURES / "article.html").read_bytes()),
        "/hostile": (200, "text/html; charset=utf-8", (FIXTURES / "hostile.html").read_bytes()),
        "/elsewhere": (302, "text/plain", b""),
    }
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def server_hits() -> list[str]:
    """The request counter, read after the fact."""
    return _Handler.hits


# ------------------------------------------------------------ the fetch side


@pytest.fixture
def article_page() -> bytes:
    return (FIXTURES / "article.html").read_bytes()


@pytest.fixture
def hostile_page() -> bytes:
    return (FIXTURES / "hostile.html").read_bytes()
