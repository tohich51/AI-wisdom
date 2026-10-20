"""Realm templates and six actual checks against the current Keycloak/gateway.

Live tests use exact-ID synthetic fixtures in the existing knowledge-hub realm.
They fail when their required provider or gateway capability is unavailable.
Credentials remain in process; no test stand or permanent users are provisioned.
The foreign-issuer case tests the verifier with a real current-realm token, not
an uncreated foreign realm. Low-level protocol regressions are in separate files.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator

import jwt
import pytest
from keycloak_admin import (
    GATEWAY_REDIRECT_URI,
    PRODUCT_CLIENT,
    REALM,
    SYNTHETIC_PREFIX,
    LiveKeycloak,
    SyntheticClient,
    new_run_id,
)

pytestmark = pytest.mark.integration

DEPLOY = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "identity"


# ---------------------------------------------------------------- runs here


def test_the_realm_template_carries_no_secret() -> None:
    """The one property of the deliverable that needs no provider."""
    text = (DEPLOY / "realm-knowledge-hub.json").read_text(encoding="utf-8")
    document = json.loads(text)  # it must also be valid JSON for the importer
    client = document["clients"][0]

    for placeholder in (
        "${KB_OIDC_CLIENT_SECRET}",
        "${KB_SMTP_PASSWORD}",
        "${KB_SMTP_USER}",
        "${KB_MAIL_FROM}",
    ):
        assert placeholder in text, placeholder

    # nothing that looks like a real credential
    assert "password" not in client
    assert "secret" in client  # ...only as a ${VAR} reference
    assert str(client["secret"]).startswith("${")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"secret"') or stripped.startswith('"password"'):
            assert "${" in stripped, stripped


def test_the_realm_client_offers_only_the_code_flow() -> None:
    document = json.loads((DEPLOY / "realm-knowledge-hub.json").read_text(encoding="utf-8"))
    client = document["clients"][0]

    assert client["publicClient"] is False  # confidential: the gateway holds the token
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert client["directAccessGrantsEnabled"] is False  # no password grant
    assert client["serviceAccountsEnabled"] is False  # no shared machine token
    assert client["frontchannelLogout"] is False
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["attributes"]["backchannel.logout.url"] == "${KB_OIDC_BACKCHANNEL_LOGOUT_URL}"
    assert document["registrationAllowed"] is False  # invite only
    assert "users" not in document  # the product ships empty
    assert document["accessTokenLifespan"] <= 600  # short-lived, per ACCESS-MODEL 7


def test_the_environment_template_is_placeholders_only() -> None:
    text = (DEPLOY / "oidc.env.example").read_text(encoding="utf-8")
    for name in ("KB_OIDC_CLIENT_SECRET", "KB_IDENTITY_KEY", "KB_SMTP_PASSWORD"):
        assert f"{name}=__" in text, name
    assert "KB_SESSION_COOKIE_SECURE=true" in text
    # two different origins, so the __Host- session cookie is never offered to
    # the identity provider
    assert "https://idp." in text and "https://kb." in text


# --------------------------------------------------------- against the live server


@pytest.fixture(scope="module")
def live_keycloak() -> Iterator[LiveKeycloak]:
    """The running Keycloak, with the host's credentials read in-process."""
    with LiveKeycloak() as instance:
        yield instance


@pytest.fixture(scope="module")
def synthetic_client(live_keycloak: LiveKeycloak) -> Iterator[SyntheticClient]:
    """A ``test-e03-*`` service client, created here and deleted by exact id.

    The id is printed so a run can be reported by name and id, and the
    teardown asserts the object is gone afterwards — a leaked synthetic client
    is a finding, not something to leave for someone to notice.
    """
    run_id = new_run_id()
    name = f"{SYNTHETIC_PREFIX}svc-{run_id}"
    client = live_keycloak.create_service_client(run_id, name)
    print(
        f"\n[c07-task1] run_id={run_id} created clientId={client.client_id} "
        f"internalId={client.internal_id} audienceMapperId={client.mapper_internal_id}"
    )
    yield client

    status = live_keycloak.delete_client_by_id(client.internal_id)
    remaining = {c["clientId"] for c in live_keycloak.clients()}
    print(
        f"[c07-task1] deleted clientId={client.client_id} "
        f"internalId={client.internal_id} http={status}"
    )
    assert client.client_id not in remaining, "the synthetic client outlived its test"
    assert client.internal_id not in {c["id"] for c in live_keycloak.clients()}


@pytest.mark.requires_keycloak
def test_the_live_realm_publishes_the_configured_issuer_and_scopes_a_token_to_its_client(
    live_keycloak: LiveKeycloak, synthetic_client: SyntheticClient
) -> None:
    """Read the realm that is actually running, and take a real token from it.

    This is a **read**, not an import. Nothing in the realm is created,
    modified or re-imported by this test; the single object it creates is its
    own ``test-e03-*`` fixture client, deleted again at teardown.

    Asserts: the realm on the server is the one the product is configured
    against; the product client offers only the code flow with S256 PKCE; the
    loopback Admin REST and the public issuer are the same server; and a token
    minted for the synthetic client verifies against the realm's live JWKS
    with ``iss`` equal to the configured issuer and ``aud`` containing that
    client's id.
    """
    # -- 1. the realm, read back from the running server -------------------
    realm = live_keycloak.realm()
    assert realm["realm"] == REALM
    assert realm["enabled"] is True
    assert realm["sslRequired"] == "external"
    assert realm["registrationAllowed"] is False  # invite only, as the template says
    assert realm["resetPasswordAllowed"] is False
    assert realm["loginWithEmailAllowed"] is True
    assert realm["bruteForceProtected"] is True
    assert realm["accessTokenLifespan"] == 300  # short-lived, per ACCESS-MODEL 7
    assert realm["otpPolicyType"] == "totp"

    clients = {c["clientId"]: c for c in live_keycloak.clients()}
    assert PRODUCT_CLIENT in clients, sorted(clients)
    gateway = clients[PRODUCT_CLIENT]
    assert gateway["publicClient"] is False
    assert gateway["standardFlowEnabled"] is True
    assert gateway["implicitFlowEnabled"] is False
    assert gateway["directAccessGrantsEnabled"] is False  # no password grant
    assert gateway["serviceAccountsEnabled"] is False
    assert gateway["attributes"]["pkce.code.challenge.method"] == "S256"
    assert gateway["redirectUris"] == [GATEWAY_REDIRECT_URI]

    # -- 2. loopback and the public issuer are one server -------------------
    loopback = live_keycloak.discovery(public=False)
    public = live_keycloak.discovery(public=True)
    assert loopback["issuer"] == public["issuer"] == live_keycloak.issuer
    assert public["token_endpoint"].startswith(live_keycloak.issuer)
    assert "S256" in public["code_challenge_methods_supported"]

    # -- 3. a real token, for a synthetic client of this run ---------------
    token = live_keycloak.client_credentials_token(synthetic_client)
    claims = jwt.decode(token.reveal(), options={"verify_signature": False}, audience=None)

    assert claims["iss"] == live_keycloak.issuer
    audience = claims["aud"]
    audience = [audience] if isinstance(audience, str) else list(audience)
    assert synthetic_client.client_id in audience, audience
    assert claims["azp"] == synthetic_client.client_id
    assert claims["typ"] == "Bearer"  # RFC 9068, what Keycloak >= 24 emits
    assert claims["exp"] - claims["iat"] == 300

    # -- 4. it is genuinely signed by this realm, not merely well formed ---
    header = jwt.get_unverified_header(token.reveal())
    assert header["alg"] == "RS256"
    jwks = live_keycloak.jwks(public)
    signing_key = next((k for k in jwks["keys"] if k["kid"] == header["kid"]), None)
    assert signing_key is not None, "the token's kid is not in the realm's JWKS"
    verified = jwt.decode(
        token.reveal(),
        key=jwt.PyJWK.from_dict(signing_key).key,
        algorithms=["RS256"],
        audience=synthetic_client.client_id,
        issuer=live_keycloak.issuer,
    )
    assert verified["sub"] == claims["sub"]
    assert verified["iss"] == live_keycloak.issuer


# ------------------------------------------------------ real browser/provider flows


@pytest.mark.requires_keycloak
def test_an_interactive_login_mints_a_token_this_gateway_accepts(live_keycloak) -> None:
    """Real browser code + PKCE login and default-deny for a nonmember."""
    from keycloak_live_checks import check_interactive

    check_interactive(live_keycloak)


@pytest.mark.requires_keycloak
def test_a_real_signed_token_is_rejected_for_a_foreign_expected_issuer(
    live_keycloak: LiveKeycloak, synthetic_client: SyntheticClient
) -> None:
    """Compare a current real token with a different expected issuer.

    This neither mints a token in a foreign realm nor exercises the product
    gateway end to end.
    """
    from uuid import UUID

    import httpx

    from kb.access.identity import IdentityRejected, IdentitySettings, TokenVerifier

    discovery = live_keycloak.discovery(public=True)
    assert discovery["issuer"] == live_keycloak.issuer
    token = live_keycloak.client_credentials_token(synthetic_client)

    with httpx.Client(timeout=20.0) as http:
        current = TokenVerifier(
            IdentitySettings(
                issuer=live_keycloak.issuer,
                audience=synthetic_client.client_id,
                client_id=synthetic_client.client_id,
                jwks_url=discovery["jwks_uri"],
            ),
            http,
        )
        accepted = current.verify(token.reveal(), require_scopes=False)
        assert accepted.issuer == live_keycloak.issuer
        assert accepted.principal_id == UUID(accepted.subject)

        foreign = TokenVerifier(
            IdentitySettings(
                issuer=live_keycloak.issuer + "/foreign",
                audience=synthetic_client.client_id,
                client_id=synthetic_client.client_id,
                jwks_url=discovery["jwks_uri"],
            ),
            http,
        )
        with pytest.raises(IdentityRejected) as exc:
            foreign.verify(token.reveal(), require_scopes=False)

    assert exc.value.code == "issuer_mismatch"


@pytest.mark.requires_keycloak
def test_password_grant_is_refused_by_the_realm(live_keycloak) -> None:
    """Use a real, browser-proven password and confidential product client."""
    from keycloak_password_check import check_password_grant

    check_password_grant(live_keycloak)


@pytest.mark.requires_keycloak
def test_an_mfa_configured_user_completes_the_flow(live_keycloak) -> None:
    """Real OTP enrollment, wrong-code rejection and successful challenge."""
    from keycloak_mfa_check import check_mfa

    check_mfa(live_keycloak)


@pytest.mark.requires_keycloak
def test_logout_at_the_provider_invalidates_the_session_locally(live_keycloak) -> None:
    """Real provider delivery revokes two devices and preserves an unrelated user."""
    from keycloak_logout_check import check_provider_logout

    check_provider_logout(live_keycloak)


@pytest.mark.requires_keycloak
def test_candidate_authlib_flow_exchanges_a_real_code_and_shares_only_pg_state(
    live_keycloak,
    make_store,
) -> None:
    """Real existing realm; local disposable PG, no production gateway session."""
    import datetime as dt
    from urllib.parse import parse_qs, urlsplit
    from uuid import UUID, uuid4

    import httpx
    from keycloak_admin import Secret
    from keycloak_browser import post_form
    from keycloak_user import synthetic_user

    from kb.access.identity import IdentitySettings, TokenVerifier
    from kb.access.session import Sealer
    from kb.http.auth_config import AuthSettings
    from kb.http.auth_flow import AuthorizationCodeFlow, LoginRejected

    product = next(c for c in live_keycloak.clients() if c["clientId"] == PRODUCT_CLIENT)
    secret = Secret(
        live_keycloak.admin_json(
            "GET",
            f"/admin/realms/{REALM}/clients/{product['id']}/client-secret",
        )["value"]
    )
    account = uuid4()
    settings = AuthSettings(
        issuer=live_keycloak.issuer,
        audience=PRODUCT_CLIENT,
        client_id=PRODUCT_CLIENT,
        client_secret=secret.reveal(),
        identity_key=Sealer.generate(),
        redirect_uri=GATEWAY_REDIRECT_URI,
    )
    first, second = make_store("authlib-live-a"), make_store("authlib-live-b")
    with synthetic_user(live_keycloak, account_id=str(account)) as user:
        with (
            httpx.Client(timeout=20, follow_redirects=False) as browser,
            httpx.Client(
                timeout=20,
                follow_redirects=False,
            ) as transport,
        ):
            verifier = TokenVerifier(
                IdentitySettings(
                    issuer=settings.issuer,
                    audience=settings.audience,
                    client_id=settings.client_id,
                ),
                transport,
            )
            a = AuthorizationCodeFlow(
                settings=settings,
                verifier=verifier,
                store=first,
                client=transport,
            )
            b = AuthorizationCodeFlow(
                settings=settings,
                verifier=verifier,
                store=second,
                client=transport,
            )
            started = a.start(return_to="/app/libraries", now=dt.datetime.now(dt.UTC))
            page = browser.get(started.authorization_url)
            response = post_form(
                browser,
                page,
                "kc-form-login",
                {
                    "username": user.username,
                    "password": user.password.reveal(),
                },
            )
            assert response.status_code == 302, "synthetic login did not return a code"
            callback = urlsplit(response.headers["location"])
            registered = urlsplit(GATEWAY_REDIRECT_URI)
            assert (callback.scheme, callback.netloc, callback.path) == (
                registered.scheme,
                registered.netloc,
                registered.path,
            )
            query = parse_qs(callback.query)
            state_matches = query.get("state") == [started.state]
            assert state_matches, "provider state mismatch"
            completed = b.complete(
                code=query["code"][0],
                state=started.state,
                binding=started.browser_binding,
                now=dt.datetime.now(dt.UTC),
            )
            loaded = first.load(completed.issued.cookie_value, now=dt.datetime.now(dt.UTC))
            assert loaded.principal_id == UUID(user.internal_id) and loaded.account_id == account
            assert completed.return_to == "/app/libraries"
            with pytest.raises(LoginRejected):
                a.complete(
                    code=query["code"][0],
                    state=started.state,
                    binding=started.browser_binding,
                    now=dt.datetime.now(dt.UTC),
                )


@pytest.mark.requires_keycloak
def test_two_personal_identities_receive_real_pkce_tokens_and_one_refreshes(
    live_keycloak,
) -> None:
    """Two synthetic people each receive real product code+PKCE tokens.

    Each person logs in through their own isolated browser session, exchanges
    one real authorization code with PKCE at the public token endpoint, and the
    current TokenVerifier accepts the result for the product audience only; one
    refresh_token is redeemed once. Codes, tokens and responses stay in process
    and are never logged or asserted raw.
    """
    import secrets
    from contextlib import ExitStack
    from urllib.parse import parse_qs, urlsplit
    from uuid import UUID, uuid4

    import httpx
    from authlib.oauth2.client import OAuth2Client
    from keycloak_admin import Secret
    from keycloak_browser import post_form
    from keycloak_user import synthetic_user

    from kb.access.identity import IdentityRejected, IdentitySettings, TokenVerifier

    product = next(c for c in live_keycloak.clients() if c["clientId"] == PRODUCT_CLIENT)
    product_secret = Secret(
        live_keycloak.admin_json(
            "GET",
            f"/admin/realms/{REALM}/clients/{product['id']}/client-secret",
        )["value"]
    )
    issuer = live_keycloak.issuer
    token_endpoint = issuer + "/protocol/openid-connect/token"
    registered = urlsplit(GATEWAY_REDIRECT_URI)
    account_id = str(uuid4())

    def code_tokens(transport, browser, user):
        """One real browser login plus PKCE exchange -> (access, refresh, nonce)."""
        code_verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        state = secrets.token_urlsafe(24)
        oauth = OAuth2Client(
            session=transport,
            client_id=PRODUCT_CLIENT,
            client_secret=product_secret.reveal(),
            scope="openid",
            redirect_uri=GATEWAY_REDIRECT_URI,
            code_challenge_method="S256",
        )
        url, _ = oauth.create_authorization_url(
            issuer + "/protocol/openid-connect/auth",
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
        )
        has_pkce = "code_challenge_method=S256" in url and "code_challenge=" in url
        assert has_pkce, "authorization request did not carry PKCE S256"
        page = browser.get(url)
        response = post_form(
            browser,
            page,
            "kc-form-login",
            {"username": user.username, "password": user.password.reveal()},
        )
        assert response.status_code == 302, "synthetic login did not return a code"
        callback = urlsplit(response.headers["location"])
        assert (callback.scheme, callback.netloc, callback.path) == (
            registered.scheme,
            registered.netloc,
            registered.path,
        ), "code did not reach the registered callback"
        query = parse_qs(callback.query)
        state_matches = query.get("state") == [state]
        assert state_matches, "provider state mismatch"
        exchanged = transport.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": Secret(query["code"][0]).reveal(),
                "redirect_uri": GATEWAY_REDIRECT_URI,
                "code_verifier": code_verifier,
            },
            auth=(PRODUCT_CLIENT, product_secret.reveal()),
        )
        assert exchanged.status_code == 200, "authorization_code exchange did not succeed"
        body = exchanged.json()
        return Secret(body["access_token"]), Secret(body["refresh_token"]), nonce

    with ExitStack() as stack:
        transport = stack.enter_context(httpx.Client(timeout=20, follow_redirects=False))
        verifier = TokenVerifier(
            IdentitySettings(issuer=issuer, audience=PRODUCT_CLIENT, client_id=PRODUCT_CLIENT),
            transport,
        )
        foreign = TokenVerifier(
            IdentitySettings(
                issuer=issuer,
                audience=secrets.token_urlsafe(18),
                client_id=PRODUCT_CLIENT,
            ),
            transport,
        )
        subjects: list[str] = []
        refreshes: list[Secret] = []
        for _ in range(2):
            user = stack.enter_context(
                synthetic_user(live_keycloak, mfa=False, account_id=account_id)
            )
            browser = stack.enter_context(httpx.Client(timeout=20, follow_redirects=False))
            access, refresh, _nonce = code_tokens(transport, browser, user)
            principal = verifier.verify(access.reveal(), require_scopes=False)
            assert UUID(str(principal.principal_id)) == UUID(user.internal_id), (
                "subject is not the person"
            )
            assert UUID(str(principal.account_id)) == UUID(account_id), "account claim is missing"
            with pytest.raises(IdentityRejected) as refused:
                foreign.verify(access.reveal(), require_scopes=False)
            assert refused.value.code == "audience_mismatch", "foreign audience was not refused"
            subjects.append(user.internal_id)
            refreshes.append(refresh)
        assert subjects[0] != subjects[1], "the two people share one subject"

        renewed_response = transport.post(
            token_endpoint,
            data={"grant_type": "refresh_token", "refresh_token": refreshes[0].reveal()},
            auth=(PRODUCT_CLIENT, product_secret.reveal()),
        )
        assert renewed_response.status_code == 200, "refresh_token grant did not succeed"
        renewed = Secret(renewed_response.json()["access_token"])
        principal = verifier.verify(renewed.reveal(), require_scopes=False)
        assert UUID(str(principal.principal_id)) == UUID(subjects[0]), "refresh changed the subject"
        assert UUID(str(principal.account_id)) == UUID(account_id), "refresh lost the account"
