"""A2 — one session resolve per request, on a lease that can be reused.

CONTRACTS.md gives this module one line: *one session resolve per request; no
cross-request permission or session cache*, with CSRF and RLS preserved.
ARCHITECTURE-PLAN.md adds the reason the resolve has to reuse a connection
instead of opening one per read, and forbids mixing credentials, roles and
``SET LOCAL``.

The resolver used to ask the store twice. ``require_principal`` loaded the
session to answer "who is this, and did they send the CSRF token"; the
resolver then loaded it again to build a ``VerifiedIdentity``. That was two
``UPDATE kb.browser_session ... RETURNING`` statements, two connections and two
idle-window extensions per request, and a window in which the CSRF answer and
the identity came from two different reads of one row.

What is asserted here, on a real PostgreSQL and a real FastAPI app:

* one request resolves the session once, and the row is written once;
* two requests resolve twice — the memo lives on ``request.state`` and nothing
  survives it, so a revoke takes effect on the very next request;
* CSRF, expiry, revocation and the membership re-check all still refuse;
* the store leases from the application's own connection factory, and the
  unprivileged gateway role it takes does not outlive that lease.

Nothing here stands in for the store or the pool. The two wrappers below only
watch: the SQL they collect runs against a real PostgreSQL, and each of them
has to honour the same lease contract the application honours.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from collections.abc import Iterator
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from kb.access.identity import VerifiedIdentity
from kb.access.session import CSRF_COOKIE, SessionLeaseMisuse
from kb.http.auth_config import AuthSettings
from kb.http.auth_routes import session_cookie_name
from kb.runtime_identity import build_connection_factory, create_resolver

pytestmark = pytest.mark.integration

UTC = dt.UTC
CSRF = "x-kb-csrf"
ISSUER = "http://127.0.0.1:65535/realms/knowledge-hub"


# ------------------------------------------------------------- the observers


class _Cursor:
    """A cursor that writes down the SQL crossing it, then gets out of the way."""

    def __init__(self, inner: Any, ledger: list[str]) -> None:
        self._inner = inner
        self._ledger = ledger

    def execute(self, query: Any, params: Any = None) -> Any:
        self._ledger.append(" ".join(str(query).split()))
        if params is None:
            return self._inner.execute(query)
        return self._inner.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __enter__(self) -> _Cursor:
        self._inner.__enter__()
        return self

    def __exit__(self, *exc: object) -> bool:
        return bool(self._inner.__exit__(*exc))


class _LeasedConnection:
    """A connection that reports its statements, and otherwise is the real one.

    ``row_factory`` is a property here on purpose: the store sets it for the
    length of its lease and restores it afterwards, and forwarding only the
    reads would break that for whoever borrows this connection next.
    """

    def __init__(self, inner: Any, ledger: list[str]) -> None:
        self._inner = inner
        self._ledger = ledger

    @property
    def row_factory(self) -> Any:
        return self._inner.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._inner.row_factory = value

    def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:
        return _Cursor(self._inner.cursor(*args, **kwargs), self._ledger)

    def execute(self, query: Any, params: Any = None) -> Any:
        self._ledger.append(" ".join(str(query).split()))
        if params is None:
            return self._inner.execute(query)
        return self._inner.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RecordingFactory:
    """The shared connection factory, with a ledger of leases and statements.

    Calling it leases nothing, as the contract requires; the count moves when
    the returned context manager is entered.
    """

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.leases = 0
        self.statements: list[str] = []

    def __call__(self) -> Any:
        return self._lease()

    @contextlib.contextmanager
    def _lease(self) -> Iterator[_LeasedConnection]:
        self.leases += 1
        with self._factory() as conn:
            yield _LeasedConnection(conn, self.statements)

    def count(self, needle: str) -> int:
        return sum(1 for statement in self.statements if needle in statement)

    def take(self) -> list[str]:
        taken, self.statements = self.statements, []
        return taken

    def reset(self) -> None:
        self.leases = 0
        self.statements = []


def _session_resolves(statements: list[str]) -> int:
    """How many times a session row was resolved by these statements.

    Matched on ``last_seen_at``, which only :meth:`SessionStore.load` writes:
    the revocation and purge statements touch the same table and would
    otherwise be counted as resolves.
    """
    return sum(1 for statement in statements if "SET last_seen_at" in statement)


# ------------------------------------------------------------------ fixtures


class _UnusedBearerVerifier:
    """The cookie path must never reach for a token verifier."""

    def verify(self, token: str) -> Any:  # pragma: no cover - a call here is the bug
        raise AssertionError("the cookie path must not verify a bearer token")


def _verified(subject: str, account: uuid.UUID) -> VerifiedIdentity:
    now = dt.datetime.now(UTC)
    return VerifiedIdentity(
        issuer=ISSUER,
        subject=subject,
        principal_id=uuid.UUID(subject),
        account_id=account,
        scopes=frozenset({"openid"}),
        issued_at=now,
        expires_at=now + dt.timedelta(hours=1),
        provider_session_id=f"kc-session-{subject}",
    )


def _app(resolve: Any) -> FastAPI:
    """The smallest application that answers with the resolved principal.

    Scaffolding, as in test_browser_flow: the subject here is the resolver,
    not a content route.
    """

    app = FastAPI()

    @app.get("/api/me")
    def me(request: Request) -> dict:
        principal = resolve(request)
        return {
            "principal_id": str(principal.principal_id),
            "account_id": str(principal.account_id),
            "issuer": request.state.identity.issuer,
        }

    @app.get("/api/me-twice")
    def me_twice(request: Request) -> dict:
        # Two dependencies in one request: the same answer, one resolve.
        first, second = resolve(request), resolve(request)
        return {"same": first == second, "principal_id": str(first.principal_id)}

    @app.post("/api/libraries")
    def create(request: Request) -> dict:
        principal = resolve(request)
        return {"created_by": str(principal.principal_id)}

    return app


def _add_member(dsn: str, account: uuid.UUID, subject: str) -> None:
    """The membership row the request-time re-check reads."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("INSERT INTO kb.organisation (id, name) VALUES (%s, 'a2-org')", (account,))
        conn.execute(
            "INSERT INTO kb.membership "
            "(organisation_id, principal_id, issuer, subject, status) "
            "VALUES (%s, %s, %s, %s, 'active')",
            (account, uuid.UUID(subject), ISSUER, subject),
        )


def _open_pool(dsn: str, *, max_size: int):
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(dsn, min_size=1, max_size=max_size, open=False)
    pool.open(wait=True)
    return pool


def _signed_in_client(resolve: Any, settings: AuthSettings, cookie: str, csrf: str):
    client = TestClient(_app(resolve), base_url="http://gateway.example")
    client.cookies.set(session_cookie_name(settings), cookie)
    client.cookies.set(CSRF_COOKIE, csrf)
    return client


@pytest.fixture
def gateway(identity_db, pool, make_store, auth_settings: AuthSettings):
    """A gateway as the application assembles it: one pool, one store, one resolver."""
    connections = RecordingFactory(build_connection_factory(pool))
    store = make_store("gateway-a")
    resolve, _token = create_resolver(connections, store, auth_settings, _UnusedBearerVerifier())

    account, subject = uuid.uuid4(), str(uuid.uuid4())
    _add_member(identity_db, account, subject)
    issued = store.create_session(_verified(subject, account), now=dt.datetime.now(UTC))

    client = _signed_in_client(resolve, auth_settings, issued.cookie_value, issued.csrf_token)
    connections.reset()
    try:
        yield SimpleNamespace(
            client=client,
            store=store,
            connections=connections,
            pool=pool,
            issued=issued,
            account=account,
            subject=subject,
            now=dt.datetime.now(UTC),
            settings=auth_settings,
        )
    finally:
        client.close()


@pytest.fixture
def pool(identity_db):
    pool = _open_pool(identity_db, max_size=2)
    try:
        yield pool
    finally:
        pool.close()


# ------------------------------------------------- one resolve, per request


def test_one_request_resolves_the_session_once(gateway):
    response = gateway.client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["issuer"] == ISSUER
    statements = gateway.connections.take()
    # The one statement that proves it: the session row is resolved once, so
    # the idle window is extended once and the row is locked once.
    assert _session_resolves(statements) == 1
    # Two leases and no more: the resolve, then the membership re-check that
    # every request has to make again.
    assert gateway.connections.leases == 2
    assert any("FROM kb.membership" in statement for statement in statements)


def test_a_write_resolves_the_session_once_and_still_checks_csrf(gateway):
    principal_id = gateway.client.get("/api/me").json()["principal_id"]
    gateway.connections.reset()

    refused = gateway.client.post("/api/libraries", json={"name": "x"})

    assert refused.status_code == 403
    # The CSRF answer came from the one load, and that load really happened:
    # a refused write still resolves the session, it just never gets a principal.
    assert _session_resolves(gateway.connections.take()) == 1
    assert gateway.connections.leases == 1

    accepted = gateway.client.post(
        "/api/libraries",
        json={"name": "x"},
        headers={CSRF: gateway.client.cookies.get(CSRF_COOKIE)},
    )

    assert accepted.status_code == 200
    assert accepted.json()["created_by"] == principal_id
    assert _session_resolves(gateway.connections.take()) == 1


def test_two_requests_resolve_twice(gateway):
    """No cross-request cache. The memo is on ``request.state`` and dies with
    the request, so the next request reads the row again."""

    assert gateway.client.get("/api/me").status_code == 200
    assert gateway.client.get("/api/me").status_code == 200

    assert _session_resolves(gateway.connections.take()) == 2


def test_one_request_that_resolves_twice_still_resolves_the_session_once(gateway):
    """Two dependencies in one request are the request-scoped memo, not a cache."""

    response = gateway.client.get("/api/me-twice")

    assert response.status_code == 200
    assert response.json()["same"] is True
    assert _session_resolves(gateway.connections.take()) == 1
    assert gateway.connections.leases == 2


# ------------------------------------------------- what must still refuse


def test_a_revoked_session_is_refused_on_the_next_request(gateway):
    assert gateway.client.get("/api/me").status_code == 200
    gateway.connections.reset()

    assert gateway.store.revoke(gateway.issued.session.id, reason="logout", now=gateway.now) is True
    refused = gateway.client.get("/api/me")

    assert refused.status_code == 401
    # The resolve still happened, and it is the row that refused: the same
    # statement that resolves a live session matches no row for a revoked one.
    assert _session_resolves(gateway.connections.take()) == 1


def test_a_forged_cookie_resolves_nothing(gateway):
    cookie_name = session_cookie_name(gateway.settings)
    real = gateway.client.cookies.get(cookie_name)
    session_id, _secret = real.split(".")
    gateway.client.cookies.set(cookie_name, f"{session_id}.{'0' * 43}")

    assert gateway.client.get("/api/me").status_code == 401


def test_a_csrf_token_from_another_session_is_refused(gateway, make_store):
    other = make_store("gateway-b").create_session(
        _verified(str(uuid.uuid4()), uuid.uuid4()), now=dt.datetime.now(UTC)
    )
    gateway.connections.reset()

    response = gateway.client.post(
        "/api/libraries", json={"name": "x"}, headers={CSRF: other.csrf_token}
    )

    assert response.status_code == 403
    assert _session_resolves(gateway.connections.take()) == 1


def test_an_expired_session_is_refused(identity_db, pool, make_store, auth_settings):
    connections = RecordingFactory(build_connection_factory(pool))
    account, subject = uuid.uuid4(), str(uuid.uuid4())
    _add_member(identity_db, account, subject)
    store = make_store("gateway-a")
    issued = store.create_session(
        _verified(subject, account), now=dt.datetime.now(UTC) - dt.timedelta(minutes=40)
    )
    resolve, _token = create_resolver(connections, store, auth_settings, _UnusedBearerVerifier())
    client = _signed_in_client(resolve, auth_settings, issued.cookie_value, issued.csrf_token)
    try:
        assert client.get("/api/me").status_code == 401
        assert _session_resolves(connections.take()) == 1
    finally:
        client.close()


def test_the_membership_re_check_still_refuses_a_session_without_one(
    identity_db, pool, make_store, auth_settings
):
    """The resolve is one load. It is not the only check a request makes."""

    def build(with_member: bool):
        connections = RecordingFactory(build_connection_factory(pool))
        account, subject = uuid.uuid4(), str(uuid.uuid4())
        if with_member:
            _add_member(identity_db, account, subject)
        store = make_store("gateway-a")
        issued = store.create_session(_verified(subject, account), now=dt.datetime.now(UTC))
        resolve, _token = create_resolver(
            connections, store, auth_settings, _UnusedBearerVerifier()
        )
        return _signed_in_client(resolve, auth_settings, issued.cookie_value, issued.csrf_token)

    allowed, refused = build(True), build(False)
    try:
        assert allowed.get("/api/me").status_code == 200
        assert refused.get("/api/me").status_code == 403
    finally:
        allowed.close()
        refused.close()


# ------------------------------------------------- the lease, and the role


def test_ten_requests_run_on_one_pooled_connection(identity_db, make_store, auth_settings):
    """The whole of the card, counted: one resolve per request, every lease
    from the one pool, and the store never answering with a connection of its
    own."""

    pool = _open_pool(identity_db, max_size=1)
    factory = build_connection_factory(pool)
    try:
        connections = RecordingFactory(factory)
        store = make_store("gateway-a")
        account, subject = uuid.uuid4(), str(uuid.uuid4())
        _add_member(identity_db, account, subject)
        issued = store.create_session(_verified(subject, account), now=dt.datetime.now(UTC))
        resolve, _token = create_resolver(
            connections, store, auth_settings, _UnusedBearerVerifier()
        )
        client = _signed_in_client(resolve, auth_settings, issued.cookie_value, issued.csrf_token)
        try:
            connections.reset()
            with factory() as conn:
                before_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                conn.rollback()
            requests_before = pool.get_stats()["requests_num"]

            for _ in range(10):
                assert client.get("/api/me").status_code == 200

            statements = connections.take()
            assert _session_resolves(statements) == 10
            # One lease for the resolve and one for the membership re-check,
            # per request, and no second connection for either of them.
            assert connections.leases == 20
            assert sum(1 for s in statements if "SET LOCAL ROLE kb_app" in s) == 10

            # Counted before the witness below, which leases once itself.
            stats = pool.get_stats()
            assert stats["requests_num"] - requests_before == 20
            assert stats["connections_num"] == 1
            # The pool only carries a counter it has had to increment, so an
            # absent one is an absent error, not an unread value.
            assert stats.get("connections_errors", 0) == 0
            assert stats.get("requests_errors", 0) == 0
            assert stats.get("connections_lost", 0) == 0

            # The same PostgreSQL backend served the whole run. That is the
            # reuse, observed rather than inferred from a counter.
            with factory() as conn:
                after_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                conn.rollback()
            assert after_pid == before_pid
        finally:
            client.close()
    finally:
        pool.close()


def test_the_gateway_role_does_not_outlive_the_lease(identity_db, make_store, auth_settings):
    """The store reads credential material as ``kb_app``. On a pooled
    connection that has to be ``SET LOCAL`` inside the lease: a session-scoped
    ``SET ROLE`` would answer the next request as the unprivileged role and
    quietly change what every policy decides on."""

    pool = _open_pool(identity_db, max_size=1)
    try:
        connections = RecordingFactory(build_connection_factory(pool))
        store = make_store("gateway-a")
        account, subject = uuid.uuid4(), str(uuid.uuid4())
        _add_member(identity_db, account, subject)
        issued = store.create_session(_verified(subject, account), now=dt.datetime.now(UTC))
        resolve, _token = create_resolver(
            connections, store, auth_settings, _UnusedBearerVerifier()
        )
        client = _signed_in_client(resolve, auth_settings, issued.cookie_value, issued.csrf_token)
        try:
            assert client.get("/api/me").status_code == 200
            assert connections.count("SET LOCAL ROLE kb_app") == 1
            assert connections.count("SET ROLE") == 0

            # The pool holds exactly one connection, so this is the same
            # physical connection the store borrowed a moment ago.
            assert pool.get_stats()["connections_num"] == 1
            with connections() as conn:
                assert conn.execute("SELECT current_user").fetchone()[0] != "kb_app"
                assert conn.execute("SELECT current_setting('app.principal', true)").fetchone() in {
                    ("",),
                    (None,),
                }
        finally:
            client.close()
    finally:
        pool.close()


def test_a_lease_inside_another_transaction_is_refused(identity_db, make_store):
    """The role belongs to one lease. Handed a connection already inside
    somebody else's transaction, the store cannot make the role local to its
    own work, so it refuses instead of downgrading the caller's transaction."""

    store = make_store("gateway-a")
    issued = store.create_session(
        _verified(str(uuid.uuid4()), uuid.uuid4()), now=dt.datetime.now(UTC)
    )
    with psycopg.connect(identity_db, autocommit=False, row_factory=dict_row) as conn:
        conn.execute("SELECT 1")  # an open transaction that is not the store's
        store.use_shared_connections(lambda: nullcontext(conn))

        with pytest.raises(SessionLeaseMisuse):
            store.load(issued.cookie_value, now=dt.datetime.now(UTC))


def test_an_unbound_store_still_reads_the_same_rows(make_store):
    """The shared factory is an addition, not a precondition: a store nobody
    bound keeps opening the connection it always opened."""

    store = make_store("gateway-a")
    issued = store.create_session(
        _verified(str(uuid.uuid4()), uuid.uuid4()), now=dt.datetime.now(UTC)
    )

    assert store.load(issued.cookie_value, now=dt.datetime.now(UTC)).id == issued.session.id


def test_binding_happens_at_wiring_and_leases_nothing(pool, make_store, auth_settings):
    """The single auth owner binds once, when the resolver is built, and the
    binding itself takes no connection."""

    connections = RecordingFactory(build_connection_factory(pool))
    store = make_store("gateway-a")

    resolve, resolve_token = create_resolver(
        connections, store, auth_settings, _UnusedBearerVerifier()
    )

    assert callable(resolve) and callable(resolve_token)
    assert connections.leases == 0
    assert connections.statements == []


def test_a_resolver_built_without_a_store_fails_closed(pool, auth_settings):
    """The binding is duck-typed, because the bearer path never needs a store.
    A store that cannot answer must still fail closed rather than admit."""

    resolve, _token = create_resolver(
        build_connection_factory(pool), object(), auth_settings, _UnusedBearerVerifier()
    )
    client = _signed_in_client(resolve, auth_settings, "a-cookie", "a-token")
    try:
        assert client.get("/api/me").status_code == 503
    finally:
        client.close()
