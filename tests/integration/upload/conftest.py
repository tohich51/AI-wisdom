"""C10 — integration fixtures. A real PostgreSQL 16.2, a real filesystem, no mocks.

Three things here are worth explaining, because each was forced by something real.

**A dedicated database.** ``migrations/0004_uploads.sql`` is a single new file on
this branch, and the C06 and C09 suites each apply their own migration set in a
session fixture. Re-applying is not available: ``CREATE POLICY`` and
``CREATE TRIGGER`` are not idempotent, and re-applying aborts halfway with an
error that names a policy rather than the real cause. So C10 creates its own
database on the shared real server and applies 0001 → 0004 there once. One
server, two schemas of state, no ordering dependency.

**The data directory stays local.** Inherited from ``tests/conftest.py``: the
workspace is a NAS mount and ``initdb`` cannot lock there. The object store is
the same story for the same reason — ``tmp_path`` is a local temporary
directory, so the bytes a test uploads never touch the repository and never
end up in a backup of it.

**The authentication middleware below is a stand-in and is not authentication.**
A real deployment verifies a signed Keycloak token at exactly that point and sets
``request.state.principal`` from the verified subject, never before. There is no
Keycloak in this environment (E03 pending), so the stand-in accepts a
well-formed UUID subject and refuses anything else. Nothing in this card's
routers reads identity from the request, so what is being tested does not depend
on the stand-in being real.
"""

from __future__ import annotations

import pathlib
import uuid
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from kb.access.policy import Principal
from kb.catalog.storage import LocalBlobStore

# this file is tests/integration/upload/conftest.py, so parents[3] is ROOT
ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
C10_DB = "kb_c10"

# Not a principal: the account every principal in the harness belongs to.
ACCOUNT = UUID("00000000-0000-4000-8000-0000000000aa")


class SqlRunner:
    """Runs one statement on a fresh connection as a chosen role and principal.

    Every call opens and closes its own connection, which is what makes the
    identity assertions meaningful: nothing is left over from a previous test
    and there is no session-level setting to forget to clear. A statement the
    server refuses comes back as rc=1 with the server's own SQLSTATE and
    message, so a test can assert on the reason rather than on psql formatting.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def __call__(
        self,
        statement: str,
        *,
        role: str | None = None,
        principal: UUID | None = None,
        params: tuple | None = None,
    ) -> tuple[int, str]:
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                if role is not None:
                    conn.execute("SET ROLE " + sql.Identifier(role).as_string(None))  # type: ignore[arg-type]
                if principal is not None:
                    conn.execute("SELECT set_config('app.principal', %s, false)", (str(principal),))
                cur = conn.execute(statement, params)
                return 0, _render(cur)
        except psycopg.Error as exc:
            return 1, f"[{exc.sqlstate}] {exc}"


def _render(cur: psycopg.Cursor) -> str:
    if cur.description is None:
        return ""
    return "\n".join(
        "\t".join("" if value is None else str(value) for value in row) for row in cur.fetchall()
    )


@pytest.fixture(scope="session")
def c10_dsn(pg_server) -> str:
    """A dedicated database on the shared real server. Created, never reused."""
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C10_DB,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C10_DB)))
    return make_conninfo(base, dbname=C10_DB)


@pytest.fixture(scope="session")
def schema(c10_dsn: str) -> bool:
    """Apply 0001 → 0004 ONCE per session. Structure only, no content.

    One migration file added by this card, and the apply order is the sorted
    glob, which is why 0004 lands last and after 0003 has created the type
    registry its ``kb.library.kind`` trigger depends on.
    """
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c10_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture(scope="session")
def run_sql(schema, c10_dsn: str) -> SqlRunner:
    return SqlRunner(c10_dsn)


@pytest.fixture(scope="session")
def admin(schema, c10_dsn: str):
    """A direct owner connection for arranging fixtures.

    This is the *migration* role, not the runtime role. Seeding as an owner is
    how a library nobody can read gets created at all, and every assertion about
    what a *caller* may see still goes through ``app.principal`` as ``kb_app``.
    """
    with psycopg.connect(c10_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def db_pool(schema, c10_dsn: str):
    """A real connection pool for the HTTP layer, connected as ``kb_app``.

    kb_app and never the migration superuser: a superuser BYPASSES RLS, so a
    pool connected as postgres would make every access assertion in this suite
    pass vacuously. The production gateway connects as kb_app for the same
    reason.
    """
    from kb.access.policy import clear_identity

    def _reset(conn: psycopg.Connection) -> None:
        conn.rollback()
        clear_identity(conn)
        conn.commit()

    pool = ConnectionPool(
        c10_dsn,
        min_size=1,
        max_size=4,
        open=True,
        reset=_reset,
        kwargs={"autocommit": False, "options": "-c role=kb_app"},
    )
    pool.wait(timeout=30)
    yield pool
    pool.close()


# --------------------------------------------------------------- object store


@pytest.fixture
def store(tmp_path) -> LocalBlobStore:
    """A real blob store on a local temp directory.

    ``tmp_path`` is local, never the NAS-backed workspace, so a multi-megabyte
    upload in a test cannot slow the repository or end up in a backup of it.
    """
    return LocalBlobStore(tmp_path / "objects")


# -------------------------------------------------------------- fixtures world


class World:
    """Arranges a scenario as the migration role.

    Seeding as the owner is how a *closed* library gets created: there is no way
    to make one through the product's own API, because a library's creator always
    becomes its manager. That is correct behaviour, so the test world needs the
    higher hand. This class never answers a question about access; it only
    arranges the rows the questions are about.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._org: UUID | None = None

    def organisation(self) -> UUID:
        if self._org is None:
            self._org = uuid.uuid4()
            self.conn.execute(
                "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
                (self._org, f"c10-org-{self._org.hex[:8]}"),
            )
        return self._org

    def library(
        self, *, name: str, kind: str = "reference", audience_scope: str = "private"
    ) -> UUID:
        lib = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind, audience_scope) "
            "VALUES (%s, %s, %s, %s, %s)",
            (lib, self.organisation(), f"{name}-{lib.hex[:8]}", kind, audience_scope),
        )
        return lib

    def grant(self, library_id: UUID, principal_id: UUID, role: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) "
            "VALUES (%s, %s, %s) ON CONFLICT (library_id, principal_id) "
            "DO UPDATE SET role = EXCLUDED.role",
            (library_id, principal_id, role),
        )

    def open_library(self, *, name: str, who: UUID, role: str = "contributor") -> UUID:
        """A library with a real grant — the ordinary, allowed case."""
        lib = self.library(name=name)
        self.grant(lib, who, role)
        return lib

    def closed_library(self, *, name: str) -> UUID:
        """A library with no grant at all. Nothing in the product creates one."""
        return self.library(name=name)

    def revoke(self, library_id: UUID, principal_id: UUID) -> None:
        self.conn.execute(
            "DELETE FROM kb.library_grant WHERE library_id = %s AND principal_id = %s",
            (library_id, principal_id),
        )

    def count(self, statement: str, params: tuple = ()) -> int:
        row = self.conn.execute(statement, params).fetchone()
        return int(row[0]) if row else 0

    # Every query below is scoped to the library a test created. The `admin`
    # connection is session-scoped and the database is shared by the whole
    # suite, so an unscoped "count every source" would make each test assert
    # about its predecessors as well as about itself.

    def sources_in(self, library_id: UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT id, object_key, content_hash FROM kb.source "
                "WHERE library_id = %s ORDER BY created_at",
                (library_id,),
            ).fetchall()
        )

    def manifests_for(self, library_id: UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT m.object_key, m.source_id, m.byte_size, m.media_type "
                "FROM kb.object_manifest m JOIN kb.source s ON s.id = m.source_id "
                "WHERE s.library_id = %s ORDER BY m.created_at",
                (library_id,),
            ).fetchall()
        )

    def ingests_in(self, library_id: UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT state, content_hash, source_id, source_url, retrieved_at "
                "FROM kb.ingest WHERE library_id = %s ORDER BY created_at",
                (library_id,),
            ).fetchall()
        )

    def versions_of(self, source_id: UUID) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT version_no, content_hash FROM kb.source_version "
                "WHERE source_id = %s ORDER BY version_no",
                (source_id,),
            ).fetchall()
        )

    def grant_rows(self) -> list[tuple]:
        return list(
            self.conn.execute(
                "SELECT library_id, principal_id, role FROM kb.library_grant"
            ).fetchall()
        )


@pytest.fixture
def world(admin) -> World:
    return World(admin)


class People:
    """One test's cast, minted fresh so no test can observe another's rows."""

    def __init__(self) -> None:
        self.owner = uuid.uuid4()
        self.colleague = uuid.uuid4()
        self.stranger = uuid.uuid4()
        self.account = uuid.uuid4()

    def principal(self, who: UUID, *, watermark: int = 1) -> Principal:
        return Principal(principal_id=who, account_id=self.account, generation_watermark=watermark)


@pytest.fixture
def people() -> People:
    return People()


@pytest.fixture
def key() -> str:
    """A fresh idempotency key. Short and stable; the DB requires eight chars."""
    return f"idem-{uuid.uuid4().hex}"


# ------------------------------------------------------------------ http api

AUTH_HEADER = "X-Test-Auth-Subject"


def build_app(pool: ConnectionPool, store: LocalBlobStore | None = None, **state):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from kb.http import libraries as libraries_http
    from kb.http import uploads as uploads_http

    app = FastAPI(title="c10-uploads-under-test")
    app.state.db_pool = pool
    app.state.blob_store = store
    for key_name, value in state.items():
        setattr(app.state, key_name, value)

    @app.middleware("http")
    async def stand_in_for_keycloak(request: Request, call_next):
        subject = request.headers.get(AUTH_HEADER)
        if subject is not None:
            try:
                resolved = uuid.UUID(subject)
            except ValueError:
                return JSONResponse({"detail": "malformed subject"}, status_code=401)
            request.state.principal = Principal(
                principal_id=resolved, account_id=ACCOUNT, generation_watermark=1
            )
        return await call_next(request)

    app.include_router(libraries_http.router)
    app.include_router(uploads_http.router)
    return app


class ApiClient:
    """A real ASGI client over a real pool and a real store. ``as_`` re-authenticates."""

    def __init__(self, app, principal: UUID | None = None) -> None:
        from fastapi.testclient import TestClient

        self._principal = principal
        self._client = TestClient(app)

    def as_(self, principal: UUID | None) -> ApiClient:
        return ApiClient(self._client.app, principal)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        merged = dict(extra or {})
        if self._principal is not None:
            merged.setdefault(AUTH_HEADER, str(self._principal))
        return merged

    def get(self, url: str, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.get(url, **kw)

    def post(self, url: str, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.post(url, **kw)

    def delete(self, url: str, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.delete(url, **kw)


@pytest.fixture
def make_api(db_pool, store):
    """Build a client over the real pool and store.

    ``pool`` and ``store`` may be overridden, which is how
    ``test_the_original_survives_a_restart_of_the_gateway`` builds a second
    application over brand new objects. Extra keyword arguments are set on
    ``app.state`` verbatim — that is how the URL transport seam is filled in for
    a test, and how the gateway would fill it in at startup.
    """

    def _make(
        principal: UUID | None = None,
        *,
        pool: ConnectionPool | None = None,
        store_: LocalBlobStore | None = None,
        **state,
    ) -> ApiClient:
        return ApiClient(build_app(pool or db_pool, store_ or store, **state), principal)

    return _make


@pytest.fixture
def api(make_api) -> ApiClient:
    """An unauthenticated client. Use ``api.as_(people.owner)`` to sign in."""
    return make_api()


@pytest.fixture
def as_owner(api, people) -> ApiClient:
    return api.as_(people.owner)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires a real PostgreSQL server")
