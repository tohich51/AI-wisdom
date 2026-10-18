from __future__ import annotations

import datetime as dt
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from fastapi import HTTPException
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from starlette.requests import Request

from kb.access.identity import VerifiedIdentity
from kb.access.session import CSRF_HEADER, BrowserPairingBusy, SessionStore
from kb.runtime_identity import create_resolver

pytestmark = pytest.mark.integration

UTC = dt.UTC
ISSUER = "https://synthetic.invalid/realms/knowledge-hub"
SUPPORTED_SCOPES = frozenset({"kb:read", "kb:contribute", "kb:experience", "kb:export", "kb:admin"})


@pytest.fixture
def store_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def _synthetic_identity(now: dt.datetime) -> VerifiedIdentity:
    principal_id = uuid.uuid4()
    account_id = uuid.uuid4()
    subject = str(principal_id)
    return VerifiedIdentity(
        issuer=ISSUER,
        subject=subject,
        principal_id=principal_id,
        account_id=account_id,
        scopes=SUPPORTED_SCOPES | {"openid"},
        issued_at=now,
        expires_at=now + dt.timedelta(minutes=90),
        provider_session_id=f"synthetic-{uuid.uuid4()}",
    )


@pytest.fixture
def verified_identity(store_now: dt.datetime) -> VerifiedIdentity:
    return _synthetic_identity(store_now)


def _approve(
    store: SessionStore,
    pairing,
    identity: VerifiedIdentity,
    now: dt.datetime,
    *,
    scopes: frozenset[str] = SUPPORTED_SCOPES,
    cloud: bool = False,
    credential_expires_at: dt.datetime | None = None,
) -> bool:
    return store.approve_browser_pairing(
        pairing.id,
        pairing.nonce,
        identity,
        authority_scopes=scopes,
        authority_cloud_client=cloud,
        credential_expires_at=(
            identity.expires_at if credential_expires_at is None else credential_expires_at
        ),
        now=now,
    )


def _issue_derived_session(
    store: SessionStore,
    identity: VerifiedIdentity,
    now: dt.datetime,
    *,
    scopes: frozenset[str],
):
    pairing = store.begin_browser_pairing(return_to="/api/v1/libraries", now=now)
    assert _approve(store, pairing, identity, now, scopes=scopes, cloud=True) is True
    approval = store.consume_browser_pairing(
        pairing.id,
        pairing.nonce,
        binding=pairing.binding,
        now=now,
    )
    assert approval is not None
    created = store.create_session(
        approval.identity,
        now=now,
        authority_scopes=approval.authority_scopes,
        authority_cloud_client=approval.authority_cloud_client,
        credential_expires_at=approval.credential_expires_at,
    )
    return created, approval


def test_browser_pairing_stays_pending_and_bad_credentials_never_grant(
    make_store, store_now, verified_identity
):
    store = make_store("pairing-pending")
    pairing = store.begin_browser_pairing(return_to="/api/v1/libraries", now=store_now)

    assert isinstance(pairing.id, uuid.UUID)
    assert pairing.nonce
    assert pairing.binding
    assert pairing.expires_at == store_now + dt.timedelta(seconds=120)
    assert (
        store.consume_browser_pairing(
            pairing.id, pairing.nonce, binding=pairing.binding, now=store_now
        )
        is None
    )
    assert (
        store.consume_browser_pairing(
            pairing.id, pairing.nonce, binding="wrong-" + pairing.binding, now=store_now
        )
        is None
    )
    assert (
        store.consume_browser_pairing(
            pairing.id, "wrong-" + pairing.nonce, binding=pairing.binding, now=store_now
        )
        is None
    )
    assert (
        store.consume_browser_pairing(
            pairing.id,
            pairing.nonce,
            binding=pairing.binding,
            now=pairing.expires_at - dt.timedelta(microseconds=1),
        )
        is None
    )

    assert _approve(store, pairing, verified_identity, store_now + dt.timedelta(seconds=1)) is True
    assert (
        store.consume_browser_pairing(
            pairing.id,
            pairing.nonce,
            binding=pairing.binding,
            now=store_now + dt.timedelta(seconds=2),
        )
        is not None
    )

    expired = store.begin_browser_pairing(return_to="/api/v1/libraries", now=store_now)
    expired_now = expired.expires_at + dt.timedelta(microseconds=1)
    assert (
        store.consume_browser_pairing(
            expired.id, expired.nonce, binding=expired.binding, now=expired_now
        )
        is None
    )
    assert _approve(store, expired, verified_identity, expired_now) is False


def test_concurrent_pairing_approvals_have_one_winner_and_cannot_replace_identity(
    make_store, store_now, verified_identity
):
    pairing = make_store("pairing-origin").begin_browser_pairing(
        return_to="/api/v1/libraries", now=store_now
    )
    identities = (verified_identity, _synthetic_identity(store_now))
    stores = (make_store("approval-a"), make_store("approval-b"))
    barrier = threading.Barrier(2)

    def approve(index):
        barrier.wait(timeout=10)
        return index, _approve(
            stores[index], pairing, identities[index], store_now + dt.timedelta(seconds=1)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, (0, 1)))

    assert sum(result for _, result in results) == 1
    winner = next(index for index, result in results if result)
    approval = make_store("approval-reader").consume_browser_pairing(
        pairing.id,
        pairing.nonce,
        binding=pairing.binding,
        now=store_now + dt.timedelta(seconds=2),
    )
    assert approval is not None
    assert approval.identity == identities[winner]
    assert approval.identity != identities[1 - winner]


def test_approved_pairing_is_consumed_once_across_restarts_and_replays_are_denied(
    make_store, store_now, verified_identity
):
    origin = make_store("pairing-consume-origin")
    pairing = origin.begin_browser_pairing(return_to="/libraries", now=store_now)
    assert _approve(origin, pairing, verified_identity, store_now, cloud=True) is True

    gateways = (make_store("pairing-restart-a"), make_store("pairing-restart-b"))
    assert (
        gateways[0].consume_browser_pairing(
            pairing.id, pairing.nonce, binding="wrong-" + pairing.binding, now=store_now
        )
        is None
    )
    assert (
        gateways[0].consume_browser_pairing(
            pairing.id, "wrong-" + pairing.nonce, binding=pairing.binding, now=store_now
        )
        is None
    )

    barrier = threading.Barrier(2)

    def consume(index):
        barrier.wait(timeout=10)
        return gateways[index].consume_browser_pairing(
            pairing.id, pairing.nonce, binding=pairing.binding, now=store_now
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        consumed = list(pool.map(consume, (0, 1)))

    assert sum(result is not None for result in consumed) == 1
    approval = next(result for result in consumed if result is not None)
    assert approval.identity == verified_identity
    assert approval.authority_scopes == SUPPORTED_SCOPES
    assert approval.authority_cloud_client is True
    assert approval.credential_expires_at == verified_identity.expires_at
    assert approval.return_to == "/libraries"

    for gateway in gateways:
        assert (
            gateway.consume_browser_pairing(
                pairing.id,
                pairing.nonce,
                binding=pairing.binding,
                now=store_now + dt.timedelta(seconds=1),
            )
            is None
        )


def test_derived_session_load_preserves_authority_and_caps_deadlines(
    make_store, store_now, verified_identity
):
    store = make_store("session-origin")
    normal_issued = store.create_session(
        verified_identity,
        now=store_now,
        authority_scopes=None,
        authority_cloud_client=False,
        credential_expires_at=None,
    )
    derived_cookie, approval = _issue_derived_session(
        store, verified_identity, store_now, scopes=SUPPORTED_SCOPES
    )

    restarted = make_store("session-restarted")
    normal = restarted.load(normal_issued.cookie_value, now=store_now)
    derived = restarted.load(derived_cookie.cookie_value, now=store_now)

    assert normal.authority_scopes is None
    assert normal.authority_cloud_client is False
    assert normal.to_principal().cloud_client is False
    assert normal.absolute_expires_at == store_now + dt.timedelta(hours=12)

    assert derived.authority_scopes == SUPPORTED_SCOPES
    assert derived.authority_cloud_client is True
    assert derived.to_principal().cloud_client is True
    assert derived.idle_expires_at > store_now
    assert derived.absolute_expires_at > store_now
    assert derived.idle_expires_at <= min(approval.credential_expires_at, normal.idle_expires_at)
    assert derived.absolute_expires_at <= min(
        approval.credential_expires_at, normal.absolute_expires_at
    )


@pytest.mark.parametrize(
    "invalid",
    ["empty", "unknown", "missing-read", "expired-cap"],
)
def test_browser_approval_refuses_invalid_authority_ceilings(
    make_store, store_now, verified_identity, invalid
):
    store = make_store(f"pairing-ceiling-{invalid}")
    pairing = store.begin_browser_pairing(return_to="/api/v1/libraries", now=store_now)

    if invalid == "empty":
        scopes = frozenset()
        credential_expires_at = verified_identity.expires_at
    elif invalid == "unknown":
        scopes = SUPPORTED_SCOPES | {"kb:unknown"}
        credential_expires_at = verified_identity.expires_at
    elif invalid == "missing-read":
        scopes = frozenset({"kb:contribute", "kb:experience", "kb:export", "kb:admin"})
        credential_expires_at = verified_identity.expires_at
    else:
        scopes = SUPPORTED_SCOPES
        credential_expires_at = store_now - dt.timedelta(seconds=1)

    with pytest.raises(ValueError):
        _approve(
            store,
            pairing,
            verified_identity,
            store_now,
            scopes=scopes,
            credential_expires_at=credential_expires_at,
        )
    assert (
        store.consume_browser_pairing(
            pairing.id, pairing.nonce, binding=pairing.binding, now=store_now
        )
        is None
    )
    assert _approve(store, pairing, verified_identity, store_now + dt.timedelta(seconds=1)) is True


@pytest.fixture
def active_membership(identity_db, store_now, verified_identity):
    identity = verified_identity
    with psycopg.connect(identity_db, autocommit=True) as admin:
        admin.execute(
            "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
            (identity.account_id, f"browser-pairing-{identity.account_id}"),
        )
        admin.execute(
            """
            INSERT INTO kb.membership
                (organisation_id, principal_id, issuer, subject, status)
            VALUES (%s, %s, %s, %s, 'active')
            """,
            (
                identity.account_id,
                identity.principal_id,
                identity.issuer,
                identity.subject,
            ),
        )

    def revoke():
        with psycopg.connect(identity_db, autocommit=True) as admin:
            admin.execute(
                """
                DELETE FROM kb.membership
                WHERE organisation_id=%s AND principal_id=%s AND issuer=%s AND subject=%s
                """,
                (
                    identity.account_id,
                    identity.principal_id,
                    identity.issuer,
                    identity.subject,
                ),
            )

    return identity, revoke


def _unused_bearer_verifier(_token: str) -> VerifiedIdentity:
    raise AssertionError("the cookie path must not invoke the bearer verifier")


@pytest.fixture
def resolver_runtime(identity_db, make_store, auth_settings):
    runtime_dsn = make_conninfo(identity_db, user="kb_app")

    def connections():
        return psycopg.connect(runtime_dsn, row_factory=dict_row)

    store = make_store("browser-resolver")
    resolve, _ = create_resolver(connections, store, auth_settings, _unused_bearer_verifier)
    return store, resolve


def _cookie_names(settings):
    def choose(names, default):
        for name in names:
            value = getattr(settings, name, None)
            if isinstance(value, str) and value:
                return value
        return default

    return (
        choose(("session_cookie_name", "session_cookie", "cookie_name"), "kb_session"),
        choose(("csrf_cookie_name", "csrf_cookie"), "kb_csrf"),
    )


def _browser_request(
    settings,
    session_cookie: str,
    csrf_cookie: str,
    method: str,
    path: str,
) -> Request:
    method = method.upper()
    session_name, csrf_name = _cookie_names(settings)
    headers = [
        (b"host", b"knowledge-hub.test"),
        (
            b"cookie",
            f"{session_name}={session_cookie}; {csrf_name}={csrf_cookie}".encode("ascii"),
        ),
    ]
    if method not in {"GET", "HEAD"}:
        headers.extend(
            (
                (CSRF_HEADER.lower().encode("ascii"), csrf_cookie.encode("ascii")),
                (b"content-type", b"application/json"),
                (b"content-length", b"0"),
            )
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("knowledge-hub.test", 443),
        }
    )


def test_resolved_read_keeps_owner_cloud_state_then_fresh_request_sees_revocation(
    auth_settings, active_membership, resolver_runtime, store_now
):
    identity, revoke = active_membership
    store, resolve = resolver_runtime
    created, _ = _issue_derived_session(store, identity, store_now, scopes=frozenset({"kb:read"}))
    session_cookie, csrf_cookie = created.cookie_value, created.csrf_token

    for _ in range(2):
        request = _browser_request(
            auth_settings, session_cookie, csrf_cookie, "GET", "/api/v1/libraries"
        )
        principal = resolve(request)
        assert principal.principal_id == identity.principal_id
        assert principal.account_id == identity.account_id
        assert principal.cloud_client is True
        assert request.state.identity.scopes == frozenset({"kb:read"})

    revoke()
    fresh_request = _browser_request(
        auth_settings, session_cookie, csrf_cookie, "GET", "/api/v1/libraries"
    )
    with pytest.raises(HTTPException) as rejected:
        resolve(fresh_request)
    assert rejected.value.status_code == 403


@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("POST", "contribute"),
        ("GET", "admin"),
        ("GET", "export"),
        ("GET", "unknown"),
    ],
    ids=["contribute", "admin", "export", "unknown-api-route"],
)
def test_resolver_refuses_every_operation_outside_derived_read_ceiling(
    auth_settings,
    active_membership,
    resolver_runtime,
    store_now,
    method,
    route,
):
    identity, _ = active_membership
    store, resolve = resolver_runtime
    created, _ = _issue_derived_session(store, identity, store_now, scopes=frozenset({"kb:read"}))
    session_cookie, csrf_cookie = created.cookie_value, created.csrf_token

    if route == "contribute":
        path = "/api/v1/libraries"
    elif route == "admin":
        path = f"/api/v1/orgs/{identity.account_id}/members"
    elif route == "export":
        path = f"/api/v1/sources/{uuid.uuid4()}/original"
    else:
        path = "/api/v1/not-registered"

    request = _browser_request(auth_settings, session_cookie, csrf_cookie, method, path)
    if method == "POST":
        assert request.headers[CSRF_HEADER] == csrf_cookie
    with pytest.raises(HTTPException) as rejected:
        resolve(request)
    assert rejected.value.status_code == 403


def test_pairing_admission_is_atomic_bounded_and_expired_rows_are_purged(make_store):
    # Move beyond all earlier synthetic challenges in this shared fixture DB.
    now = dt.datetime.now(UTC) + dt.timedelta(hours=1)
    store = make_store("pairing-quota")
    for _ in range(63):
        store.begin_browser_pairing(return_to=None, now=now)
    barrier = threading.Barrier(2)

    def begin(index):
        barrier.wait(timeout=10)
        try:
            make_store(f"pairing-quota-{index}").begin_browser_pairing(return_to=None, now=now)
            return True
        except BrowserPairingBusy:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(begin, (0, 1))) == [False, True]
    with pytest.raises(BrowserPairingBusy):
        store.begin_browser_pairing(return_to=None, now=now)
    assert store.purge_browser_pairings(now=now + dt.timedelta(seconds=119)) == 0
    assert store.purge_browser_pairings(now=now + dt.timedelta(seconds=120)) == 64
    assert store.begin_browser_pairing(return_to=None, now=now + dt.timedelta(seconds=120))
