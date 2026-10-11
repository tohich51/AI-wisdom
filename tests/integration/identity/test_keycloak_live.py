"""C07 — the Keycloak leg, and the part of it that can be checked here.

**Split deliberately.** Four tests in this file run: three check the delivered
realm *template* as a file, and one checks the *running* server. The other
five are ``not_run``, because the workspace that wrote them had no JVM and
therefore no Keycloak (``docs/handoff/runtime-capabilities.json``,
``keycloak_real.available = false``). They are written out rather than left
out so the next executor can run them unchanged on a host that has a provider,
and they report as SKIPPED — never as passed.

What a skipped test means here, precisely: the assertions were never
evaluated. There is no result. Recording them as anything other than
``not_run`` in ``docs/handoff/results/C07.json`` would be a false claim, and
the same is true of the E03 gate.

The live-server test
--------------------
``test_the_live_realm_publishes_the_configured_issuer_and_scopes_a_token_to_its_client``
runs against the Keycloak 26.0.8 instance on the host. It **reads** the realm
back through Admin REST and asks it for a real token; it does not import the
realm, create a realm, or change a realm setting, and it is named for what it
does rather than for the import that used to stand in this slot. The one
object it creates is a confidential ``test-e03-*`` service client that it
deletes again by exact id, tagged with its run id.

It is not skipped when Keycloak is absent: it fails. A gate that cannot run
is ``not_run``, and the project's rule is that a silent skip is a false green.
The cost of that choice — ``just integration`` now needs a reachable Keycloak
— is recorded against C07 rather than hidden in a skip marker.
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

NOT_RUN_REASON = (
    "no Keycloak in this workspace (no JVM; runtime-capabilities.json "
    "keycloak_real.available=false). Blocked behind owner gate E03."
)


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
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
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


# ---------------------------------------------------------------- not run


@pytest.mark.requires_keycloak
@pytest.mark.skip(reason=NOT_RUN_REASON)
def test_an_interactive_login_mints_a_token_this_gateway_accepts() -> None:
    """A human logs in at Keycloak; the gateway verifies what comes back.

    Asserts: a real `code` + PKCE exchange yields an ID token that
    ``TokenVerifier`` accepts, with `iss` and `aud` matching the realm.
    """
    raise AssertionError("unreachable: the test is not_run in this workspace")


@pytest.mark.requires_keycloak
@pytest.mark.skip(reason=NOT_RUN_REASON)
def test_a_token_minted_by_another_realm_is_rejected() -> None:
    """A second realm in the same Keycloak must not be usable here.

    Asserts: an ID token whose `iss` is the other realm is refused with
    ``issuer_mismatch`` even though both realms share a signing key set.
    """
    raise AssertionError("unreachable: the test is not_run in this workspace")


@pytest.mark.requires_keycloak
@pytest.mark.skip(reason=NOT_RUN_REASON)
def test_password_grant_is_refused_by_the_realm() -> None:
    """The realm configuration, verified against the running server.

    Asserts: a password grant to ``kb-gateway`` is refused. The template says
    so; only the running server can prove it.
    """
    raise AssertionError("unreachable: the test is not_run in this workspace")


@pytest.mark.requires_keycloak
@pytest.mark.skip(reason=NOT_RUN_REASON)
def test_an_mfa_configured_user_completes_the_flow() -> None:
    """TOTP is enabled in the realm; the browser flow still completes.

    Asserts: a session is established after a TOTP challenge, and the audit log
    records no second factor value.
    """
    raise AssertionError("unreachable: the test is not_run in this workspace")


@pytest.mark.requires_keycloak
@pytest.mark.skip(reason=NOT_RUN_REASON)
def test_logout_at_the_provider_invalidates_the_session_locally() -> None:
    """The product's own logout path plus a Keycloak backchannel logout.

    Asserts: a backchannel logout for the provider session revokes the
    ``kb.browser_session`` rows that carry that ``session_state``.
    """
    raise AssertionError("unreachable: the test is not_run in this workspace")
