"""C07 — the browser flow end to end: code, PKCE, cookies, CSRF, two gateways.

Real PostgreSQL for session state, a real HTTP provider process for the
authorization-code exchange, a real FastAPI app for the routes. The only
stand-in is the identity provider itself, which is not Keycloak because
Keycloak cannot be run here — see ``conftest.py`` and
``test_keycloak_live.py``.

The test app also mounts one protected write route. It is scaffolding whose
only job is to exercise the ``require_principal`` dependency: the real content
API belongs to C04/C05 and this card does not own it.

Acceptance rows exercised here, from C07 and ACCESS-MODEL:
  * A04  — a substituted identity header changes nothing
  * A04  — a wrong issuer/audience/alg is refused (``test_token_validation``)
  * #2   — CSRF blocks a write
  * #3   — the callback and the next request work through a different gateway
  * #4   — no secret or token in any response body or log line
  * S01  — two gateways, neither holding the session in memory
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from kb.access.identity import IdentitySettings, TokenVerifier
from kb.access.policy import Principal
from kb.access.session import CSRF_COOKIE, SESSION_COOKIE, SessionMissing, SessionStore
from kb.http.auth_config import AuthSettings
from kb.http.auth_flow import AuthorizationCodeFlow
from kb.http.auth_routes import create_auth_router, require_principal, session_cookie_name

pytestmark = pytest.mark.integration

UTC = dt.UTC
LOGIN_BINDING = "kb_login"
JWT_SHAPED = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
FIXED_NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------ fixtures


def _build(store: SessionStore, settings: AuthSettings, http: httpx.Client) -> FastAPI:
    verifier = TokenVerifier(
        IdentitySettings(
            issuer=settings.issuer,
            audience=settings.audience,
            client_id=settings.client_id,
        ),
        http,
    )
    flow = AuthorizationCodeFlow(settings=settings, verifier=verifier, store=store, client=http)
    clock = lambda: FIXED_NOW  # noqa: E731 - a fixed clock keeps deadlines exact

    app = FastAPI()
    app.include_router(create_auth_router(flow=flow, store=store, settings=settings, clock=clock))

    # Scaffolding, not product code: one read and one write route behind the
    # dependency that turns a cookie into a Principal.
    guard = require_principal(store, settings, clock)

    @app.get("/api/me")
    def me(principal: Principal = Depends(guard)) -> dict:  # noqa: B008 - FastAPI dependency idiom
        return {"principal_id": str(principal.principal_id)}

    @app.post("/api/libraries")
    def create_library(
        principal: Principal = Depends(guard),  # noqa: B008 - FastAPI dependency idiom
    ) -> dict:
        return {"created_by": str(principal.principal_id)}

    return app


@pytest.fixture
def gateways(auth_settings: AuthSettings, make_store):
    """Two independent gateway processes against one database."""
    http_clients: list[httpx.Client] = []
    clients = {}
    stores = {}
    for name, label in (("a", "gateway-a"), ("b", "gateway-b")):
        http = httpx.Client(timeout=5.0)
        http_clients.append(http)
        store = make_store(label)
        stores[name] = store
        clients[name] = TestClient(_build(store, auth_settings, http), follow_redirects=False)
    try:
        yield SimpleNamespace(
            a=clients["a"],
            b=clients["b"],
            store_a=stores["a"],
            store_b=stores["b"],
            settings=auth_settings,
        )
    finally:
        for http in http_clients:
            http.close()


def query_of(url: str) -> dict[str, str]:
    """The query string as the provider will see it, after percent-decoding."""
    from urllib.parse import parse_qs, urlsplit

    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def callback_of(callback_url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(callback_url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def sign_in(gateways, provider) -> SimpleNamespace:
    """A complete login: gateway A starts it, gateway B finishes it."""
    started = gateways.a.get("/auth/login", params={"return_to": "/libraries"})
    assert started.status_code == 302
    binding = started.cookies.get(LOGIN_BINDING)
    assert binding

    with httpx.Client(timeout=5.0, follow_redirects=False) as browser:
        at_provider = browser.get(started.headers["location"])
    assert at_provider.status_code == 302

    # the browser carries the binding cookie to whichever gateway answers
    gateways.b.cookies.set(LOGIN_BINDING, binding)
    finished = gateways.b.get(callback_of(at_provider.headers["location"]))
    assert finished.status_code == 302, finished.text

    return SimpleNamespace(
        callback=finished,
        session_cookie=gateways.b.cookies.get(SESSION_COOKIE),
        csrf_cookie=gateways.b.cookies.get(CSRF_COOKIE),
        state=at_provider.request.url.params.get("state", ""),
        location=finished.headers["location"],
    )


# ------------------------------------------------------- step 1: the redirect


def test_login_redirects_to_the_provider_with_pkce_state_and_nonce(gateways, provider):
    response = gateways.a.get("/auth/login", params={"return_to": "/libraries"})
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(provider.discovery_document()["authorization_endpoint"])

    query = query_of(location)
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert "plain" not in location
    assert query["client_id"] == provider.client_id
    assert query["redirect_uri"].endswith("/auth/callback")
    assert query["code_challenge"]
    assert query["state"]
    assert query["nonce"]
    # the verifier is never sent to the provider, and neither is the secret
    assert "code_verifier" not in location
    assert provider.client_secret not in location


def test_login_sets_a_short_lived_binding_cookie(gateways):
    response = gateways.a.get("/auth/login")
    raw = response.headers["set-cookie"]
    assert LOGIN_BINDING in raw
    assert "HttpOnly" in raw
    assert "Path=/" in raw
    assert "Max-Age=600" in raw  # KB_SESSION_LOGIN_TTL_MINUTES default


def test_a_return_target_cannot_leave_the_origin(gateways):
    for hostile in ("https://evil.invalid/steal", "//evil.invalid", "/\\evil.invalid", "libraries"):
        response = gateways.a.get("/auth/login", params={"return_to": hostile})
        assert response.status_code == 302
        # the target survives the round trip only if it is a plain same-origin path
        assert query_of(response.headers["location"])["state"]


def test_a_hostile_return_target_lands_on_the_application_root(gateways, provider):
    started = gateways.a.get("/auth/login", params={"return_to": "https://evil.invalid"})
    with httpx.Client(timeout=5.0, follow_redirects=False) as browser:
        at_provider = browser.get(started.headers["location"])
    gateways.a.cookies.set(LOGIN_BINDING, started.cookies.get(LOGIN_BINDING))
    finished = gateways.a.get(callback_of(at_provider.headers["location"]))
    assert finished.status_code == 302
    assert finished.headers["location"] == "/"
    assert "evil.invalid" not in finished.headers["location"]


# ------------------------------------------- step 2: the callback, on another gateway


def test_the_callback_and_the_next_request_work_through_another_gateway(
    gateways, provider, identity_db
):
    """Acceptance #3 and S01. The login starts on gateway A, the callback lands
    on gateway B, and B serves the following request. A is never asked again
    and is then dropped."""
    done = sign_in(gateways, provider)
    assert done.callback.headers["location"] == "/libraries"
    assert done.session_cookie
    assert done.csrf_cookie
    del gateways.a

    who = gateways.b.get("/auth/session")
    assert who.status_code == 200
    assert who.json()["authenticated"] is True
    assert who.json()["principal_id"]
    assert who.json()["csrf_header"] == "x-kb-csrf"

    me = gateways.b.get("/api/me")
    assert me.status_code == 200
    assert me.json()["principal_id"] == who.json()["principal_id"]


def test_the_redirect_uri_that_came_from_the_provider_is_the_one_we_registered(gateways, provider):
    """A provider that redirects somewhere else must not get a session: the
    token endpoint is asked to confirm the same redirect_uri we stored."""
    sign_in(gateways, provider)
    posted = provider.token_requests[-1]
    assert posted["redirect_uri"] == gateways.settings.redirect_uri
    assert posted["grant_type"] == "authorization_code"
    assert posted["code_verifier"]  # the sealed verifier was unsealed and sent
    assert posted["client_secret"] == provider.client_secret


def test_a_replayed_callback_is_refused(gateways, provider):
    started = gateways.a.get("/auth/login")
    with httpx.Client(timeout=5.0, follow_redirects=False) as browser:
        at_provider = browser.get(started.headers["location"])
    path = callback_of(at_provider.headers["location"])
    binding = started.cookies.get(LOGIN_BINDING)

    gateways.a.cookies.set(LOGIN_BINDING, binding)
    first = gateways.a.get(path)
    assert first.status_code == 302

    gateways.b.cookies.set(LOGIN_BINDING, binding)
    second = gateways.b.get(path)
    assert second.status_code == 400
    assert second.json() == {"error": "state_unknown_expired_or_used"}


def test_a_callback_without_the_browser_binding_is_refused(gateways, provider):
    started = gateways.a.get("/auth/login")
    with httpx.Client(timeout=5.0, follow_redirects=False) as browser:
        at_provider = browser.get(started.headers["location"])
    # a different browser: the binding cookie never arrived
    response = gateways.b.get(callback_of(at_provider.headers["location"]))
    assert response.status_code == 400
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")
    assert gateways.b.get("/auth/session").json()["authenticated"] is False


def test_a_callback_with_a_forged_code_is_refused(gateways):
    started = gateways.a.get("/auth/login")
    state = query_of(started.headers["location"])["state"]
    gateways.a.cookies.set(LOGIN_BINDING, started.cookies.get(LOGIN_BINDING))
    response = gateways.a.get(
        "/auth/callback", params={"code": "a-code-nobody-issued", "state": state}
    )
    # the state is genuine here, so the refusal happens one step later: the
    # provider will not redeem a code it never issued
    assert response.status_code == 400
    assert response.json() == {"error": "token_exchange_rejected"}
    assert gateways.a.get("/auth/session").json()["authenticated"] is False


def test_the_token_endpoint_answer_is_verified_not_trusted(gateways, provider):
    """A provider that returns a token signed by the wrong key is refused, so
    the session is not established on the provider's word alone."""
    sign_in(gateways, provider)
    assert provider.token_requests  # the exchange really happened

    original = provider.mint_id_token

    def rogue(**kwargs):
        # signed by the wrong key, but claiming a kid the provider does
        # publish, so the refusal is the signature check and nothing else
        kwargs["key"] = provider.keys["attacker"]
        kwargs["headers"] = {"kid": "c07-key-current"}
        return original(**kwargs)

    provider.mint_id_token = rogue  # type: ignore[method-assign]
    try:
        started = gateways.a.get("/auth/login")
        with httpx.Client(timeout=5.0, follow_redirects=False) as browser:
            at_provider = browser.get(started.headers["location"])
        gateways.b.cookies.set(LOGIN_BINDING, started.cookies.get(LOGIN_BINDING))
        response = gateways.b.get(callback_of(at_provider.headers["location"]))
        assert response.status_code == 400
        assert response.json() == {"error": "signature_invalid"}
    finally:
        provider.mint_id_token = original  # type: ignore[method-assign]


# --------------------------------------------------------------- cookies


def test_the_session_cookie_is_httponly_and_the_csrf_cookie_is_not(gateways, provider):
    done = sign_in(gateways, provider)
    cookies = done.callback.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if SESSION_COOKIE in c)
    csrf_cookie = next(c for c in cookies if CSRF_COOKIE in c)

    assert "HttpOnly" in session_cookie
    assert "samesite=lax" in session_cookie.lower()
    assert "Path=/" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "samesite=lax" in csrf_cookie.lower()


def test_production_cookies_carry_the_host_prefix_and_secure(auth_settings, make_store):
    """`__Host-` makes a browser refuse the cookie if it is ever set without
    Secure, for a different path, or for another host."""
    secure = AuthSettings(
        issuer=auth_settings.issuer,
        audience=auth_settings.audience,
        client_id=auth_settings.client_id,
        client_secret=auth_settings.client_secret,
        identity_key=auth_settings.identity_key,
        redirect_uri=auth_settings.redirect_uri,
        cookie_secure=True,
    )
    with httpx.Client(timeout=5.0) as http:
        app = _build(make_store("gateway-secure"), secure, http)
        with TestClient(app, follow_redirects=False) as client:
            response = client.get("/auth/login")
    raw = response.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-kb_login=") for c in raw)
    assert all("Secure" in c for c in raw)
    assert session_cookie_name(secure).startswith("__Host-")


def test_the_binding_cookie_is_cleared_after_the_callback(gateways, provider):
    done = sign_in(gateways, provider)
    cleared = [c for c in done.callback.headers.get_list("set-cookie") if LOGIN_BINDING in c]
    assert cleared
    assert any("Max-Age=0" in c or c.startswith(f"{LOGIN_BINDING}=;") for c in cleared)


# ------------------------------------------------------------------ CSRF


def test_a_write_without_a_csrf_token_is_refused(gateways, provider):
    sign_in(gateways, provider)
    response = gateways.b.post("/api/libraries", json={"name": "x"})
    assert response.status_code == 403
    assert response.json()["detail"] == {"error": "csrf_token_required"}


def test_a_write_with_an_attacker_chosen_token_is_refused(gateways, provider):
    sign_in(gateways, provider)
    # the attacker plants both halves of their own pair. The real cookie is
    # removed first: leaving both would make this test pass for the wrong
    # reason, since a duplicate cookie is not what a browser would send.
    del gateways.b.cookies[CSRF_COOKIE]
    gateways.b.cookies.set(CSRF_COOKIE, "attacker-chosen")
    response = gateways.b.post(
        "/api/libraries", json={"name": "x"}, headers={"x-kb-csrf": "attacker-chosen"}
    )
    assert response.status_code == 403


def test_a_write_with_a_header_cookie_mismatch_is_refused(gateways, provider):
    sign_in(gateways, provider)
    response = gateways.b.post(
        "/api/libraries", json={"name": "x"}, headers={"x-kb-csrf": "not-the-cookie"}
    )
    assert response.status_code == 403


def test_a_write_with_the_matching_token_succeeds(gateways, provider):
    sign_in(gateways, provider)
    response = gateways.b.post(
        "/api/libraries",
        json={"name": "x"},
        headers={"x-kb-csrf": gateways.b.cookies.get(CSRF_COOKIE)},
    )
    assert response.status_code == 200
    assert response.json()["created_by"] == gateways.b.get("/api/me").json()["principal_id"]


def test_a_read_does_not_need_a_csrf_token(gateways, provider):
    sign_in(gateways, provider)
    assert gateways.b.get("/api/me").status_code == 200
    assert gateways.b.get("/auth/session").status_code == 200


# ------------------------------------------------- identity comes from the wire


def test_a_substituted_identity_header_changes_nothing(gateways, provider):
    """A04's third case. Whatever the caller asserts, the answer is the
    session's identity or a refusal."""
    sign_in(gateways, provider)
    real = gateways.b.get("/api/me").json()["principal_id"]

    for header, value in (
        ("x-user-id", str(uuid.uuid4())),
        ("x-forwarded-user", str(uuid.uuid4())),
        ("x-remote-user", "admin"),
        ("x-kb-principal", str(uuid.uuid4())),
    ):
        response = gateways.b.get("/api/me", headers={header: value})
        assert response.status_code == 200
        assert response.json()["principal_id"] == real


def test_a_user_id_in_the_request_body_is_data_not_identity(gateways, provider):
    sign_in(gateways, provider)
    real = gateways.b.get("/api/me").json()["principal_id"]
    response = gateways.b.post(
        "/api/libraries",
        json={"name": "x", "user_id": str(uuid.uuid4()), "role": "manager"},
        headers={"x-kb-csrf": gateways.b.cookies.get(CSRF_COOKIE)},
    )
    assert response.status_code == 200
    assert response.json()["created_by"] == real


def test_no_cookie_means_no_principal(gateways):
    assert gateways.b.get("/api/me").status_code == 401
    # FastAPI wraps an HTTPException detail; the code itself is the contract
    assert gateways.b.get("/api/me").json()["detail"] == {"error": "not_authenticated"}


def test_a_forged_session_cookie_is_refused(gateways, provider):
    sign_in(gateways, provider)
    real = gateways.b.cookies.get(SESSION_COOKIE)
    session_id, _secret = real.split(".")
    del gateways.b.cookies[SESSION_COOKIE]
    gateways.b.cookies.set(SESSION_COOKIE, f"{session_id}.{'0' * 43}")
    assert gateways.b.get("/api/me").status_code == 401


# ------------------------------------------------------------------ logout


def test_logout_revokes_the_session(gateways, provider):
    done = sign_in(gateways, provider)
    cookie = gateways.b.cookies.get(SESSION_COOKIE)
    response = gateways.b.post(
        "/auth/logout", headers={"x-kb-csrf": gateways.b.cookies.get(CSRF_COOKIE)}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "signed_out"}
    assert any("Max-Age=0" in c for c in response.headers.get_list("set-cookie"))

    # the row is revoked, so no other gateway will honour that cookie either
    with pytest.raises(SessionMissing):
        gateways.store_b.load(cookie, now=FIXED_NOW + dt.timedelta(minutes=1))
    assert done.session_cookie is not None


def test_logout_without_a_csrf_token_leaves_the_session_alone(gateways, provider):
    sign_in(gateways, provider)
    response = gateways.b.post("/auth/logout")
    assert response.status_code == 403
    assert gateways.b.get("/auth/session").json()["authenticated"] is True


def test_logging_out_twice_is_not_an_error(gateways, provider):
    sign_in(gateways, provider)
    first = gateways.b.post(
        "/auth/logout", headers={"x-kb-csrf": gateways.b.cookies.get(CSRF_COOKIE)}
    )
    assert first.status_code == 200
    second = gateways.b.post("/auth/logout")
    assert second.status_code == 200
    assert second.json() == {"status": "signed_out"}


# ------------------------------- acceptance #4: nothing leaks into API or logs


def test_no_token_or_secret_reaches_a_response_body(gateways, provider):
    seen: list[str] = []
    done = sign_in(gateways, provider)
    seen.append(done.callback.text)
    for response in (
        gateways.b.get("/auth/session"),
        gateways.b.get("/api/me"),
        gateways.b.post(
            "/api/libraries",
            json={},
            headers={"x-kb-csrf": gateways.b.cookies.get(CSRF_COOKIE)},
        ),
    ):
        seen.append(response.text)
    started = gateways.a.get("/auth/login")
    seen.append(started.text)
    with httpx.Client(timeout=5.0, follow_redirects=False) as browser:
        at_provider = browser.get(started.headers["location"])
        seen.append(at_provider.text)
    failed = gateways.b.get(callback_of(at_provider.headers["location"]))
    seen.append(failed.text)

    blob = "\n".join(seen)
    code = provider.token_requests[-1]["code"] if provider.token_requests else ""
    authorization_code = at_provider.request.url.params.get("code", "")
    assert JWT_SHAPED.search(blob) is None, blob
    for secret in (
        code,
        authorization_code,
        provider.client_secret,
        gateways.settings.client_secret,
        done.session_cookie or "unset",
        done.csrf_cookie or "unset",
    ):
        if secret:
            assert secret not in blob, secret


def test_the_authorization_code_is_not_echoed_in_the_callback_redirect(gateways, provider):
    done = sign_in(gateways, provider)
    assert "code=" not in done.location
    assert done.location == "/libraries"


def test_nothing_secret_reaches_the_log(gateways, provider, caplog):
    caplog.set_level(logging.DEBUG)
    done = sign_in(gateways, provider)
    refused = gateways.b.post("/api/libraries", json={})  # no CSRF token
    assert refused.status_code == 403
    gateways.b.post("/auth/logout", headers={"x-kb-csrf": gateways.b.cookies.get(CSRF_COOKIE)})
    failed = gateways.b.get("/auth/callback", params={"code": "x", "state": "y"})
    assert failed.status_code == 400

    haystack = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for secret in (
        provider.client_secret,
        done.session_cookie or "unset",
        done.csrf_cookie or "unset",
    ):
        assert secret not in haystack, secret
    assert JWT_SHAPED.search(haystack) is None, haystack
    # the audit trail still says what happened
    assert "login_established" in haystack
    assert "csrf_rejected" in haystack


# ------------------------------------------------------------- settings


def test_a_settings_object_does_not_print_its_secrets(auth_settings):
    rendered = repr(auth_settings) + str(auth_settings)
    assert auth_settings.client_secret not in rendered
    assert auth_settings.identity_key not in rendered
    assert "<redacted>" in rendered
    assert auth_settings.issuer in rendered


def test_a_gateway_with_placeholder_configuration_refuses_to_start():
    from kb.http.auth_config import AuthSettings as Settings
    from kb.http.auth_config import IdentityNotConfigured

    with pytest.raises(IdentityNotConfigured):
        Settings.from_env(
            {
                "KB_OIDC_ISSUER": "https://idp.invalid/realms/kb",
                "KB_OIDC_AUDIENCE": "kb-gateway",
                "KB_OIDC_CLIENT_ID": "kb-gateway",
                "KB_OIDC_CLIENT_SECRET": "__KB_OIDC_CLIENT_SECRET__",
                "KB_IDENTITY_KEY": "x" * 44,
                "KB_OIDC_REDIRECT_URI": "https://kb.invalid/auth/callback",
            }
        )
    with pytest.raises(IdentityNotConfigured):
        Settings.from_env({"KB_OIDC_ISSUER": "https://idp.invalid/realms/kb"})
    with pytest.raises(IdentityNotConfigured):
        Settings.from_env(
            {
                "KB_OIDC_ISSUER": "https://idp.invalid/realms/kb",
                "KB_OIDC_AUDIENCE": "kb-gateway",
                "KB_OIDC_CLIENT_ID": "kb-gateway",
                "KB_OIDC_CLIENT_SECRET": "real-secret",
                "KB_IDENTITY_KEY": "x" * 44,
                "KB_OIDC_REDIRECT_URI": "https://kb.invalid/auth/callback",
                "KB_SESSION_IDLE_MINUTES": "0",
            }
        )
