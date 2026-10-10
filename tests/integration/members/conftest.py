"""C08 — integration fixtures. A real PostgreSQL 16.2, real DDL, no mocks.

**A dedicated database.** The C06 suite applies every ``migrations/0*.sql`` to
the default database in a session fixture, and C09 hit the same problem: those
fixtures cannot be made idempotent from outside (``CREATE POLICY`` and
``CREATE TRIGGER`` are not idempotent) and both test files are outside this
card's allowed paths. So C08 applies the same DDL to its own database on the
same real server. One server, two schemas of state, no ordering dependency.

**The data directory stays local.** Inherited from ``tests/conftest.py``: the
workspace is a NAS mount and ``initdb`` cannot lock there.

**The identity stand-in is not authentication.** Read
:func:`build_app` before copying it. There is no Keycloak in this environment
(E03 pending). The middleware below does what C07's does at the same point: it
puts a verified identity on ``request.state`` and nothing else. What C08 tests
does not depend on it — no route, no body and no domain call in this card reads
identity from the request — but the deactivation ORDER test does depend on
something being in ``request.state`` at all, so the stand-in has to be explicit
about what it is.

**The session store stand-in is not Keycloak.** It opens its own connection to
the real database and reads ``kb.membership.status`` at the moment it is called.
That is the whole mechanism of the ordering test: it observes committed state
the way any other process would, and it is not permitted to write anything.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid
from typing import Any
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from kb.access.identity import VerifiedIdentity
from kb.access.membership_deactivation import RevocationOutcome
from kb.access.policy import Principal

# this file is tests/integration/members/conftest.py, so parents[3] is ROOT
ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
C08_DB = "kb_c08"

#: The issuer the stand-in pretends Keycloak issued tokens for. It is a
#: coordinate, not a URL anyone can reach.
TEST_ISSUER = "https://id.example.invalid/realms/kb"
ACCOUNT = UUID("00000000-0000-4000-8000-0000000000aa")
#: Only ever the audit field on a seeded row; carries no access meaning.
SEED_SUBMITTER = UUID("00000000-0000-4000-8000-000000000001")


# --------------------------------------------------------------- database


@pytest.fixture(scope="session")
def c08_dsn(pg_server) -> str:
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C08_DB,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C08_DB)))
    return make_conninfo(base, dbname=C08_DB)


@pytest.fixture(scope="session")
def schema(c08_dsn: str) -> bool:
    """Apply 0001 + 0002 + 0003 + 0004 ONCE per session. Structure only.

    Session-scoped for the reason C06 recorded: re-applying per test aborts
    halfway through on the first non-idempotent CREATE POLICY and the error
    names a policy rather than the real cause.
    """
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c08_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture(scope="session")
def run_sql(schema, c08_dsn: str):
    """Run one statement as a chosen role with a chosen transaction principal.

    A fresh connection per call, so an identity assertion cannot be satisfied
    by a session setting left over from an earlier call.
    """

    def _run(
        statement: str,
        params: tuple = (),
        *,
        role: str | None = "kb_app",
        principal: UUID | None = None,
    ) -> tuple[int, str]:
        try:
            with psycopg.connect(c08_dsn, autocommit=True) as conn:
                if role is not None:
                    conn.execute("SET ROLE " + sql.Identifier(role).as_string(None))  # type: ignore[arg-type]
                if principal is not None:
                    conn.execute("SELECT set_config('app.principal', %s, false)", (str(principal),))
                cur = conn.execute(statement, params)
                if cur.description is None:
                    return 0, ""
                rows = cur.fetchall()
                return 0, "\n".join(
                    "\t".join("" if v is None else str(v) for v in row) for row in rows
                )
        except psycopg.Error as exc:
            return 1, f"[{exc.sqlstate}] {str(exc).splitlines()[0]}"

    return _run


@pytest.fixture(scope="session")
def admin(schema, c08_dsn: str):
    """A direct owner connection for arranging fixtures.

    This is the *migration* role, not the runtime role. Seeding as an owner is
    how a closed library gets created at all: there is no way to make a library
    nobody can read through the product's own API, because the creator always
    becomes its manager. Every assertion about a caller's view still goes
    through ``app.principal`` as ``kb_app``.
    """
    with psycopg.connect(c08_dsn, autocommit=True) as conn:
        yield conn


# ------------------------------------------------------------ test world


class World:
    """Arranges a scenario as the migration role. Never answers an access question."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._org: UUID | None = None

    def organisation(self) -> UUID:
        if self._org is None:
            self._org = uuid.uuid4()
            self.conn.execute(
                "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
                (self._org, f"c08-org-{self._org.hex[:8]}"),
            )
        return self._org

    def library(self, *, kind: str = "reference", name: str | None = None) -> UUID:
        lib = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind) VALUES (%s, %s, %s, %s)",
            (lib, self.organisation(), name or f"c08-lib-{lib.hex[:8]}", kind),
        )
        return lib

    def grant(self, library_id: UUID, principal_id: UUID, role: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (library_id, principal_id) DO UPDATE SET role = EXCLUDED.role",
            (library_id, principal_id, role),
        )

    def source(self, library_id: UUID, title: str) -> UUID:
        src = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.source (id, library_id, title, media_type, submitted_by, "
            "object_key, content_hash) VALUES (%s, %s, %s, 'text/plain', %s, %s, %s)",
            (src, library_id, title, SEED_SUBMITTER, f"c08/{src.hex[:16]}", "b" * 64),
        )
        return src

    def membership(
        self, principal_id: UUID, *, status: str = "active", subject: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO kb.membership (organisation_id, principal_id, issuer, subject, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                self.organisation(),
                principal_id,
                TEST_ISSUER,
                subject or f"sub-{principal_id.hex}",
                status,
            ),
        )

    def group(self, name: str = "team") -> UUID:
        grp = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.access_group (id, organisation_id, name) VALUES (%s, %s, %s)",
            (grp, self.organisation(), name),
        )
        return grp

    def group_member(self, group_id: UUID, principal_id: UUID) -> None:
        self.conn.execute(
            "INSERT INTO kb.access_group_member (group_id, organisation_id, principal_id) "
            "VALUES (%s, (SELECT organisation_id FROM kb.access_group WHERE id = %s), %s)",
            (group_id, group_id, principal_id),
        )

    def group_grant(self, group_id: UUID, library_id: UUID, role: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.access_group_grant (group_id, organisation_id, library_id, role) "
            "VALUES (%s, (SELECT organisation_id FROM kb.access_group WHERE id = %s), %s, %s)",
            (group_id, group_id, library_id, role),
        )

    def browser_session(self, principal_id: UUID) -> UUID:
        session_id = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.browser_session (id, secret_hash, csrf_token_hash, principal_id, "
            "account_id, issuer, subject, idle_expires_at, absolute_expires_at, "
            "created_by_instance) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '30 min', "
            "now() + interval '12 hours', 'c08-test')",
            (
                session_id,
                b"s" * 32,
                b"c" * 32,
                principal_id,
                ACCOUNT,
                TEST_ISSUER,
                f"sub-{principal_id.hex}",
            ),
        )
        return session_id

    def count(self, statement: str, params: tuple = ()) -> int:
        row = self.conn.execute(statement, params).fetchone()
        return int(row[0]) if row else 0

    def membership_row(self, principal_id: UUID) -> tuple | None:
        return self.conn.execute(
            "SELECT status, deactivated_at, deactivated_by, deactivation_reason "
            "FROM kb.membership WHERE organisation_id = %s AND principal_id = %s",
            (self.organisation(), principal_id),
        ).fetchone()

    def journal(self, action: str | None = None) -> list[tuple]:
        if action is None:
            return self.conn.execute(
                "SELECT revision, action, subject_principal_id, actor_principal_id, detail "
                "FROM kb.access_policy_journal WHERE organisation_id = %s ORDER BY id",
                (self.organisation(),),
            ).fetchall()
        return self.conn.execute(
            "SELECT revision, action, subject_principal_id, actor_principal_id, detail "
            "FROM kb.access_policy_journal WHERE organisation_id = %s AND action = %s ORDER BY id",
            (self.organisation(), action),
        ).fetchall()


@pytest.fixture
def world(admin) -> World:
    return World(admin)


class People:
    """One test's cast, minted fresh so no test can observe another's rows."""

    def __init__(self) -> None:
        self.admin = uuid.uuid4()
        self.colleague = uuid.uuid4()
        self.stranger = uuid.uuid4()
        self.account = uuid.uuid4()

    def principal(self, who: UUID) -> Principal:
        return Principal(principal_id=who, account_id=self.account, generation_watermark=1)

    def identity(self, who: UUID) -> VerifiedIdentity:
        """A stand-in for what C07's middleware puts on ``request.state``."""
        return VerifiedIdentity(
            issuer=TEST_ISSUER,
            subject=f"sub-{who.hex}",
            principal_id=who,
            account_id=self.account,
            scopes=frozenset({"kb:read"}),
            issued_at=dt.datetime.now(dt.UTC),
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        )


@pytest.fixture
def people() -> People:
    return People()


# ------------------------------------------------- the deactivation stand-in


class ObservingSessionRevoker:
    """Records the call, and observes the database at the moment of the call.

    THIS IS NOT KEYCLOAK. It stands in for the second half of a deactivation
    that cannot run here (no JVM, no realm). What makes it worth having is not
    that it records a call — any object could do that — but that it opens a
    SECOND connection to the same real PostgreSQL and reads the committed value
    of ``kb.membership.status`` while it is being called.

    That is what turns the ordering rule into an observation:

    * PostgreSQL first  -> the probe sees ``deactivated``
    * Keycloak first     -> the probe sees ``active``

    The test asserts on the probe. Swapping the two steps in
    :func:`kb.access.membership_deactivation.deactivate_member` changes what it
    sees, and the test fails. Nothing here writes to the database: a stand-in
    that could close a membership would make the ordering untestable, because the
    thing being ordered would be able to do the first step itself.
    """

    def __init__(self, dsn: str, *, revoked: bool = True, reason: str = "logged_out") -> None:
        self._dsn = dsn
        self._revoked = revoked
        self._reason = reason
        self.observed: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []

    def revoke_sessions(self, *, issuer: str, subject: str) -> RevocationOutcome:
        with psycopg.connect(self._dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT m.status, m.deactivated_at, m.deactivated_by "
                "FROM kb.membership m WHERE m.issuer = %s AND m.subject = %s",
                (issuer, subject),
            ).fetchone()
        self.observed.append(
            {
                "status": row[0] if row else None,
                "deactivated_at": row[1] if row else None,
                "deactivated_by": row[2] if row else None,
            }
        )
        self.calls.append((issuer, subject))
        return RevocationOutcome(revoked=self._revoked, reason=self._reason)


class FailingSessionRevoker(ObservingSessionRevoker):
    """A provider that is simply not there. Records the same observation."""

    def __init__(self, dsn: str) -> None:
        super().__init__(dsn, revoked=False, reason="provider_unreachable")


class ExplodingSessionRevoker(ObservingSessionRevoker):
    """A provider that raises instead of answering."""

    def revoke_sessions(self, *, issuer: str, subject: str):  # type: ignore[no-untyped-def]
        super().revoke_sessions(issuer=issuer, subject=subject)
        raise TimeoutError("provider did not answer")


@pytest.fixture
def revoker(c08_dsn: str) -> ObservingSessionRevoker:
    return ObservingSessionRevoker(c08_dsn)


# ------------------------------------------------------------------ http api

#: The header the stand-in reads. A real deployment reads a verified bearer
#: token at this exact point; there is no Keycloak here to verify one against.
AUTH_HEADER = "X-Test-Auth-Subject"


def build_app(pool: ConnectionPool, *, revoker: Any = None):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from kb.http import members as members_http

    app = FastAPI(title="c08-members-under-test")
    app.state.db_pool = pool
    app.state.session_revoker = revoker

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
            request.state.identity = VerifiedIdentity(
                issuer=TEST_ISSUER,
                subject=f"sub-{resolved.hex}",
                principal_id=resolved,
                account_id=ACCOUNT,
                scopes=frozenset({"kb:read"}),
                issued_at=dt.datetime.now(dt.UTC),
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
            )
        return await call_next(request)

    app.include_router(members_http.router)
    return app


class ApiClient:
    """A real ASGI client over a real pool. ``as_`` re-authenticates."""

    def __init__(
        self, pool: ConnectionPool, principal: UUID | None = None, *, revoker: Any = None
    ) -> None:
        from fastapi.testclient import TestClient

        self._pool = pool
        self._principal = principal
        self._client = TestClient(build_app(pool, revoker=revoker))

    def as_(self, principal: UUID | None) -> ApiClient:
        return ApiClient(self._pool, principal)

    def with_revoker(self, revoker: Any) -> ApiClient:
        return ApiClient(self._pool, self._principal, revoker=revoker)

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
def db_pool(schema, c08_dsn: str) -> ConnectionPool:
    """A real connection pool for the HTTP layer, connected as ``kb_app``.

    The role matters: a superuser BYPASSES RLS, so a pool connected as
    ``postgres`` would make every access assertion in this suite pass
    vacuously. The production gateway connects as ``kb_app`` for the same
    reason.
    """

    def _reset(conn: psycopg.Connection) -> None:
        from kb.access.policy import clear_identity

        conn.rollback()
        clear_identity(conn)
        conn.commit()

    pool = ConnectionPool(
        c08_dsn,
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
def api(db_pool) -> ApiClient:
    """An unauthenticated client. Use ``api.as_(ADMIN)`` to sign in as somebody."""
    return ApiClient(db_pool)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires a real PostgreSQL server")


@pytest.fixture
def failing_revoker(c08_dsn: str) -> FailingSessionRevoker:
    """A provider that answers "no". Not Keycloak — see the class docstring."""
    return FailingSessionRevoker(c08_dsn)


@pytest.fixture
def exploding_revoker(c08_dsn: str) -> ExplodingSessionRevoker:
    """A provider that raises instead of answering."""
    return ExplodingSessionRevoker(c08_dsn)


# ------------------------------------------------- organisation + actor


@pytest.fixture
def connect(c08_dsn: str):
    """Open a fresh non-autocommit connection. The caller closes it."""

    def _open() -> psycopg.Connection:
        # role=kb_app, for the same reason db_pool sets it: a superuser BYPASSES
        # RLS, so a domain test run as postgres would assert nothing about the
        # policies it is supposed to be exercising.
        return psycopg.connect(c08_dsn, autocommit=False, options="-c role=kb_app")

    return _open


@pytest.fixture
def db(c08_dsn: str):
    """One non-autocommit connection for the whole test, closed afterwards.

    Two settings, both load-bearing:

    * ``role=kb_app`` — a superuser BYPASSES RLS. Every domain call made
      through this connection is then answered by the policies, which is the
      only reason a passing test says anything about them.
    * ``autocommit=False`` — the production gateway's setting, and what makes
      the ordering test meaningful: the commit that closes the membership is a
      real commit, and the probe connection is genuinely reading data another
      transaction wrote.
    """
    with psycopg.connect(c08_dsn, autocommit=False, options="-c role=kb_app") as conn:
        yield conn


@pytest.fixture
def organisation(world, people, run_sql) -> UUID:
    """A fresh organisation whose administrator is ``people.admin``.

    The admin row is created by ``kb.claim_organisation_admin`` — the product's
    own bootstrap path, run as ``kb_app`` under the admin's transaction-local
    principal — rather than by inserting into the table as the migration role.
    If the bootstrap stopped working, every deactivation test here would fail,
    which is the direction this dependency should point.
    """
    org = world.organisation()
    rc, out = run_sql("SELECT kb.claim_organisation_admin(%s)", (org,), principal=people.admin)
    assert rc == 0, out
    return org


class ChattySessionRevoker(ObservingSessionRevoker):
    """A provider with a lot to say, for the journal-hygiene test.

    Its ``detail`` carries a key that looks like a secret and two that are
    outside the agreed vocabulary. The journal is readable by an administrator
    and outlives the request, so only keys the product chose may reach it.
    """

    def revoke_sessions(self, *, issuer: str, subject: str) -> RevocationOutcome:
        return RevocationOutcome(
            revoked=True,
            reason="logged_out",
            detail={
                "sessions": 3,
                "client_id": "kb-gateway",
                "token": "eyJhbGciOiJSUzI1NiJ9.secret",
                "body": "x" * 5000,
                "nested": {"cannot": "happen"},
            },
        )


@pytest.fixture
def observing_revoker(c08_dsn: str) -> ObservingSessionRevoker:
    """The observer. THIS IS NOT KEYCLOAK — see the class docstring."""
    return ObservingSessionRevoker(c08_dsn)


@pytest.fixture
def observing_revoker_factory(c08_dsn: str):
    """A second observer, for a test that needs two."""

    def _make(**kwargs) -> ObservingSessionRevoker:
        return ObservingSessionRevoker(c08_dsn, **kwargs)

    return _make


@pytest.fixture
def chatty_revoker(c08_dsn: str) -> ChattySessionRevoker:
    return ChattySessionRevoker(c08_dsn)
