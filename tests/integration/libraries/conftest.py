"""C09 — integration fixtures. A real PostgreSQL 16.2, real DDL, no mocks.

Two things here are worth explaining, because both were forced by a real
failure rather than chosen for style.

**A dedicated database.** The C06 suite applies every ``migrations/0*.sql`` in a
session fixture, and that glob now picks up 0003 as well. C06's fixture cannot
be made idempotent from here — ``CREATE POLICY`` and ``CREATE TRIGGER`` are not
idempotent, and re-applying aborts halfway — and C06's test file is outside this
card's allowed paths. So C09 applies the same DDL to its own *database* on the
same real server. One server, two schemas of state, no ordering dependency: each
suite owns its catalogue, and running either alone, or both in one session,
works.

**The data directory stays local.** Inherited from ``tests/conftest.py``: the
workspace is a NAS mount and ``initdb`` cannot lock there. The shared
``pg_server`` fixture already handles that.

``schema`` is session-scoped and applies the migrations once, following the
pattern C06 established, for the same reason: re-applying per test fails
confusingly halfway through.
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

# this file is tests/integration/libraries/conftest.py, so parents[3] is ROOT
ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
C09_DB = "kb_c09"

# The shared session owns the server, not the identities. Every test mints its
# own principals (the `people` fixture), so no test can see another's libraries
# whatever order the suite runs in. These two are not principals: ACCOUNT is the
# account every principal in the harness belongs to, and SEED_SUBMITTER is only
# the audit field on a seeded source row, which carries no access meaning.
ACCOUNT = UUID("00000000-0000-4000-8000-0000000000aa")
SEED_SUBMITTER = UUID("00000000-0000-4000-8000-000000000001")


class SqlRunner:
    """Runs SQL as a chosen role with a chosen principal; returns (rc, output).

    Every call opens a *fresh* connection. That is what makes the identity
    assertions meaningful: there is no connection left over from a previous
    test, and no session-level setting to forget to clear. A denied statement
    is caught as a ``psycopg.Error`` and returned as rc=1 with the server's own
    message and SQLSTATE, so a test can assert on the reason rather than on
    incidental psql formatting.
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
                    conn.execute("SET ROLE " + _ident(role))
                if principal is not None:
                    # session-level, safe precisely because this connection is
                    # used by exactly one statement and then closed
                    conn.execute("SELECT set_config('app.principal', %s, false)", (str(principal),))
                cur = conn.execute(statement, params)
                return 0, _render(cur)
        except psycopg.Error as exc:
            return 1, f"[{exc.sqlstate}] {exc}"


def _ident(name: str) -> str:
    """Quote an identifier. The inputs are module-level constants, never
    request data, but quoting keeps the helper honest if that ever changes."""
    return sql.Identifier(name).as_string(None)  # type: ignore[arg-type]


def _render(cur: psycopg.Cursor) -> str:
    if cur.description is None:
        return ""
    return "\n".join(
        "\t".join("" if value is None else str(value) for value in row) for row in cur.fetchall()
    )


@pytest.fixture(scope="session")
def c09_dsn(pg_server) -> str:
    """A dedicated catalogue on the shared real server.

    Created, not reused, so C09's rows can never be mistaken for C06's.
    """
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C09_DB,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C09_DB)))
    return make_conninfo(base, dbname=C09_DB)


@pytest.fixture(scope="session")
def schema(c09_dsn: str) -> bool:
    """Apply 0001 + 0002 + 0003 ONCE per session. Structure only, no content."""
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c09_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture(scope="session")
def run_sql(schema, c09_dsn: str) -> SqlRunner:
    return SqlRunner(c09_dsn)


@pytest.fixture(scope="session")
def admin(schema, c09_dsn: str):
    """A direct owner connection for arranging fixtures.

    This is the *migration* role, not the runtime role. Seeding test data as an
    owner is deliberate: it keeps the tests about what RLS does to rows, and it
    is the only way to create the closed libraries the negative tests need.
    Every assertion about a caller's view still goes through ``app.principal``
    as ``kb_app``.
    """
    with psycopg.connect(c09_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def db_pool(schema, c09_dsn: str):
    """A real connection pool for the HTTP layer.

    The reset runs ``kb.access.policy.clear_identity`` on every release. The
    service path only ever sets identity with ``set_config(...,
    is_local => true)`` inside an explicit transaction, so it cannot survive a
    commit; the reset is belt and braces against a future code path that is
    lazier, and ``test_a_pooled_connection_carries_no_identity`` proves it.
    """
    from kb.access.policy import clear_identity

    def _reset(conn: psycopg.Connection) -> None:
        conn.rollback()
        clear_identity(conn)
        # clear_identity opens an implicit transaction; commit it so the next
        # checkout sees an IDLE connection and the pool does not treat this one
        # as dirty.
        conn.commit()

    pool = ConnectionPool(
        c09_dsn,
        min_size=1,
        max_size=4,
        open=True,
        reset=_reset,
        # kb_app, never the migration superuser. This is not a detail: a
        # superuser BYPASSES RLS, so a pool connected as postgres would make
        # every access assertion in this file pass vacuously — the queries would
        # see all 14 libraries for a principal granted exactly one. The
        # production gateway connects as kb_app for the same reason, and the
        # first version of this fixture did not, which is why the first run of
        # test_a_pooled_connection_carries_no_identity failed.
        kwargs={"autocommit": False, "options": "-c role=kb_app"},
    )
    pool.wait(timeout=30)
    yield pool
    pool.close()


# --------------------------------------------------------------- fixtures world


class World:
    """Arranges a scenario as the migration role.

    Seeding as the owner is how a closed library gets created at all: there is
    no way to make a library nobody can read by using the product's own API,
    because the creator always becomes its manager. That is correct behaviour,
    so the test world needs the higher hand.

    Every assertion about what a *caller* may see still goes through
    ``app.principal`` as ``kb_app``. This class never answers a question about
    access; it only arranges the rows the questions are about.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._org: UUID | None = None

    def organisation(self) -> UUID:
        if self._org is None:
            self._org = uuid.uuid4()
            self.conn.execute(
                "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
                (self._org, f"c09-org-{self._org.hex[:8]}"),
            )
        return self._org

    def library(self, *, name: str, kind: str, audience_scope: str = "private") -> UUID:
        lib = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind, audience_scope) "
            "VALUES (%s, %s, %s, %s, %s)",
            (lib, self.organisation(), name, kind, audience_scope),
        )
        return lib

    def grant(self, library_id: UUID, principal_id: UUID, role: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) "
            "VALUES (%s, %s, %s) ON CONFLICT (library_id, principal_id) "
            "DO UPDATE SET role = EXCLUDED.role",
            (library_id, principal_id, role),
        )

    def project(self, library_id: UUID, description: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO kb.project (id, description) VALUES (%s, %s)", (library_id, description)
        )

    def link(self, project_id: UUID, library_id: UUID, *, is_required: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO kb.project_library_link (project_id, library_id, is_required) "
            "VALUES (%s, %s, %s)",
            (project_id, library_id, is_required),
        )

    def source(self, library_id: UUID, title: str) -> UUID:
        src = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.source (id, library_id, title, media_type, submitted_by, "
            "object_key, content_hash) VALUES (%s, %s, %s, 'text/plain', %s, %s, %s)",
            (src, library_id, title, SEED_SUBMITTER, f"c09/{src.hex[:16]}", "a" * 64),
        )
        return src

    def rule(
        self,
        library_id: UUID,
        *,
        version_no: int = 1,
        title: str = "r",
        actions: tuple[str, ...] = ("do the first thing", "do the second thing"),
    ) -> UUID:
        rule = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.rule (id, library_id, version_no, title, when_to_apply, "
            "expected_effect, publication) VALUES (%s, %s, %s, %s, 'when', 'effect', 'published')",
            (rule, library_id, version_no, title),
        )
        # a published rule with no action steps is not a rule the contract will
        # model, so every seeded rule carries at least one real step
        for ordinal, body in enumerate(actions):
            self.conn.execute(
                "INSERT INTO kb.rule_action (rule_id, ordinal, body) VALUES (%s, %s, %s)",
                (rule, ordinal, body),
            )
        return rule

    def pin(
        self, project_id: UUID, rule_id: UUID, version_no: int, *, is_required: bool = True
    ) -> None:
        self.conn.execute(
            "INSERT INTO kb.project_rule_pin (project_id, rule_id, version_no, is_required) "
            "VALUES (%s, %s, %s, %s)",
            (project_id, rule_id, version_no, is_required),
        )

    def count(self, statement: str, params: tuple = ()) -> int:
        row = self.conn.execute(statement, params).fetchone()
        return int(row[0]) if row else 0

    def rule_title(self, rule_id: UUID) -> str:
        row = self.conn.execute("SELECT title FROM kb.rule WHERE id = %s", (rule_id,)).fetchone()
        return str(row[0]) if row else ""


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

    def principal(self, who: UUID) -> Principal:
        return Principal(principal_id=who, account_id=self.account, generation_watermark=1)


@pytest.fixture
def people() -> People:
    return People()


# ------------------------------------------------------------------ http api

# The middleware below stands in for C07's Keycloak verification. Read this
# before copying it anywhere: IT IS NOT AUTHENTICATION. A real deployment
# verifies a signed token — issuer, audience, expiry — at this exact point and
# sets request.state.principal from the verified subject, never before it. There
# is no Keycloak in this environment (E03 pending), so the stand-in accepts any
# well-formed UUID subject and refuses anything else: enough shape checking to
# catch a broken harness, and no more.
#
# What this card tests does not depend on the stand-in. No route, no request
# model and no catalogue call ever reads identity from the request. The
# middleware is the only thing in the process that looks at a request header for
# a subject, exactly as a real auth layer looks at a token.
AUTH_HEADER = "X-Test-Auth-Subject"


def build_app(pool: ConnectionPool):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from kb.http import libraries as libraries_http
    from kb.http import projects as projects_http

    app = FastAPI(title="c09-catalog-under-test")
    app.state.db_pool = pool

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
    app.include_router(projects_http.router)
    return app


class ApiClient:
    """A real ASGI client over a real pool. ``as_`` re-authenticates."""

    def __init__(self, pool: ConnectionPool, principal: UUID | None = None) -> None:
        from fastapi.testclient import TestClient

        self._pool = pool
        self._principal = principal
        self._client = TestClient(build_app(pool))

    def as_(self, principal: UUID | None) -> ApiClient:
        return ApiClient(self._pool, principal)

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

    def put(self, url: str, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.put(url, **kw)

    def delete(self, url: str, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.delete(url, **kw)


@pytest.fixture
def api(db_pool) -> ApiClient:
    """An unauthenticated client. Use ``api.as_(OWNER)`` to sign in as somebody."""
    return ApiClient(db_pool)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires a real PostgreSQL server")
