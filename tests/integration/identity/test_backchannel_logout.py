"""Real RSA/JWKS and PostgreSQL regressions; live Keycloak has separate tests."""

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.access.identity import IdentityRejected, VerifiedIdentity
from kb.access.logout import LogoutVerifier
from kb.access.session import SessionMissing
from kb.http.backchannel import create_backchannel_router

pytestmark = pytest.mark.integration
EVENT = "http://schemas.openid.net/event/backchannel-logout"


def claims(provider, now, *, sid=None, subject=None):
    result = {
        "iss": provider.issuer,
        "aud": provider.client_id,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 120,
        "jti": str(uuid4()),
        "events": {EVENT: {}},
        "sid": sid or str(uuid4()),
    }
    if subject is not None:
        result["sub"] = subject
    return result


def identity(provider, now, sid, *, subject=None):
    principal = UUID(subject) if subject else uuid4()
    return VerifiedIdentity(
        issuer=provider.issuer,
        subject=str(principal),
        principal_id=principal,
        account_id=UUID(provider.default_account_id),
        scopes=frozenset(),
        issued_at=now,
        expires_at=now + dt.timedelta(minutes=5),
        provider_session_id=sid,
    )


def test_valid_sid_only_signed_logout(identity_settings, verifier, provider, signing_keys):
    now = dt.datetime.now(dt.UTC)
    payload = claims(provider, now)
    token = signing_keys["current"].sign(payload, headers={"typ": "logout+jwt"})
    result = LogoutVerifier(identity_settings, verifier).verify(token, now=now)
    assert result.subject is None
    assert result.session_id == payload["sid"]


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://foreign.invalid/realms/other"},
        {"aud": "wrong-client"},
        {"exp": 1},
        {"iat": 9999999999},
        {"iat": -400},
        {"iat": True},
        {"events": {}},
        {"events": {EVENT: {"unexpected": True}}},
        {"events": {EVENT: []}},
        {"nonce": None},
        {"sid": ""},
        {"sub": None},
        {"jti": ""},
    ],
)
def test_signed_invalid_claims_are_rejected(
    change, identity_settings, verifier, provider, signing_keys
):
    now = dt.datetime.now(dt.UTC)
    payload = claims(provider, now)
    changes = dict(change)
    if changes.get("iat") == -400:
        changes["iat"] = int(now.timestamp()) - 400
    payload.update(changes)
    token = signing_keys["current"].sign(payload)
    with pytest.raises(IdentityRejected):
        LogoutVerifier(identity_settings, verifier).verify(token, now=now)


@pytest.mark.parametrize("missing", ["exp", "jti", "sid", "events"])
def test_missing_claim_is_rejected(missing, identity_settings, verifier, provider, signing_keys):
    now = dt.datetime.now(dt.UTC)
    payload = claims(provider, now)
    del payload[missing]
    with pytest.raises(IdentityRejected):
        LogoutVerifier(identity_settings, verifier).verify(
            signing_keys["current"].sign(payload), now=now
        )


def test_wrong_signature_and_ordinary_id_token_are_rejected(
    identity_settings, verifier, provider, signing_keys
):
    now = dt.datetime.now(dt.UTC)
    payload = claims(provider, now)
    bad_signature = signing_keys["attacker"].sign(
        payload, headers={"kid": signing_keys["current"].kid}
    )
    id_token = provider.mint_id_token(subject=str(uuid4()), nonce="ordinary-id-token")
    for token in (bad_signature, id_token):
        with pytest.raises(IdentityRejected):
            LogoutVerifier(identity_settings, verifier).verify(token, now=now)


def test_exact_scope_replay_and_late_callback(
    identity_settings, verifier, provider, signing_keys, store, make_store
):
    now = dt.datetime.now(dt.UTC)
    sid = str(uuid4())
    original = identity(provider, now, sid)
    two = [store.create_session(original, now=now) for _ in range(2)]
    other_sid = store.create_session(replace(original, provider_session_id=str(uuid4())), now=now)
    other_issuer = store.create_session(
        replace(original, issuer="https://other.invalid/realms/other"), now=now
    )
    token = signing_keys["current"].sign(claims(provider, now, sid=sid))
    event = LogoutVerifier(identity_settings, verifier).verify(token, now=now)
    second = make_store("gateway-b")
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda s: s.apply_provider_logout(event, now=now), [store, second]))
    assert sorted(counts) == [0, 2]
    for issued in two:
        with pytest.raises(SessionMissing):
            second.load(issued.cookie_value, now=now)
    for issued in (other_sid, other_issuer):
        assert second.load(issued.cookie_value, now=now).id == issued.session.id
    assert second.apply_provider_logout(event, now=now) == 0
    with pytest.raises(SessionMissing, match="provider_session_logged_out"):
        second.create_session(original, now=now)


def test_optional_subject_limits_revocation(
    identity_settings, verifier, provider, signing_keys, store
):
    now = dt.datetime.now(dt.UTC)
    sid = str(uuid4())
    target = identity(provider, now, sid)
    different = identity(provider, now, sid)
    wanted = store.create_session(target, now=now)
    preserved = store.create_session(different, now=now)
    event = LogoutVerifier(identity_settings, verifier).verify(
        signing_keys["current"].sign(claims(provider, now, sid=sid, subject=target.subject)),
        now=now,
    )
    assert store.apply_provider_logout(event, now=now) == 1
    with pytest.raises(SessionMissing):
        store.load(wanted.cookie_value, now=now)
    assert store.load(preserved.cookie_value, now=now).id == preserved.session.id
    assert store.create_session(different, now=now).session.principal_id == different.principal_id
    broad = replace(event, subject=None, jti=str(uuid4()))
    assert store.apply_provider_logout(broad, now=now) == 2
    with pytest.raises(SessionMissing):
        store.create_session(different, now=now)


def test_overlapping_callback_and_logout_cannot_leave_a_live_session(
    identity_settings, verifier, provider, signing_keys, store, make_store
):
    now = dt.datetime.now(dt.UTC)
    original = identity(provider, now, str(uuid4()))
    event = LogoutVerifier(identity_settings, verifier).verify(
        signing_keys["current"].sign(claims(provider, now, sid=original.provider_session_id)),
        now=now,
    )
    barrier = Barrier(2)
    second = make_store("racing-gateway")

    def callback():
        barrier.wait(timeout=10)
        try:
            return second.create_session(original, now=now)
        except SessionMissing:
            return None

    def logout():
        barrier.wait(timeout=10)
        return store.apply_provider_logout(event, now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        created, revoked = pool.submit(callback), pool.submit(logout)
        issued, count = created.result(timeout=15), revoked.result(timeout=15)
    if issued is None:
        assert count == 0
    else:
        assert count == 1
        with pytest.raises(SessionMissing):
            store.load(issued.cookie_value, now=now)
    with pytest.raises(SessionMissing):
        store.create_session(original, now=now)


def test_http_valid_replay_unknown_sid_and_malformed_requests(
    identity_settings, verifier, provider, signing_keys, store
):
    now = dt.datetime.now(dt.UTC)
    original = identity(provider, now, str(uuid4()))
    issued = store.create_session(original, now=now)
    app = FastAPI()
    app.include_router(
        create_backchannel_router(
            verifier=LogoutVerifier(identity_settings, verifier), store=store, clock=lambda: now
        )
    )
    with TestClient(app) as client:
        path = "/auth/backchannel-logout"
        wrong_type = client.post(path, json={"logout_token": "wrong"})
        assert wrong_type.status_code == 400
        assert store.load(issued.cookie_value, now=now).id == issued.session.id
        for kwargs in (
            {"data": {"logout_token": "wrong"}},
            {"content": "logout_token=x&logout_token=y"},
            {"content": "logout_token=" + "x" * 33000},
        ):
            response = client.post(
                path, headers={"content-type": "application/x-www-form-urlencoded"}, **kwargs
            )
            assert response.status_code == 400
            assert response.headers["cache-control"] == "no-store"
            assert store.load(issued.cookie_value, now=now).id == issued.session.id
        token = signing_keys["current"].sign(
            claims(provider, now, sid=original.provider_session_id)
        )
        for _ in range(2):
            response = client.post(path, data={"logout_token": token})
            assert response.status_code == 200
            assert response.json() == {"status": "signed_out"}
            assert response.headers["cache-control"] == "no-store"
        with pytest.raises(SessionMissing):
            store.load(issued.cookie_value, now=now)
        unknown = signing_keys["current"].sign(claims(provider, now))
        assert client.post(path, data={"logout_token": unknown}).status_code == 200
