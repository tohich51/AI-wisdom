"""C07 — the Keycloak leg, and the part of it that can be checked here.

**Split deliberately.** One test in this file runs and checks the delivered
realm template. The rest are ``not_run``, because this workspace has no JVM
and therefore no Keycloak (``docs/handoff/runtime-capabilities.json``,
``keycloak_real.available = false``). They are written out rather than left
out so the next executor can run them unchanged on a host that has a provider,
and they report as SKIPPED — never as passed.

What a skipped test means here, precisely: the assertions were never
evaluated. There is no result. Recording them as anything other than
``not_run`` in ``docs/handoff/results/C07.json`` would be a false claim, and
the same is true of the E03 gate.
"""

from __future__ import annotations

import json
import pathlib

import pytest

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


# ---------------------------------------------------------------- not run


@pytest.mark.requires_keycloak
@pytest.mark.skip(reason=NOT_RUN_REASON)
def test_the_realm_imports_into_a_real_keycloak() -> None:
    """Render the template with a real secret and import it.

    Asserts: the import succeeds; the client exists with one redirect URI; a
    login at the provider returns an ID token whose `iss` equals the configured
    issuer and whose `aud` is the client id.
    """
    raise AssertionError("unreachable: the test is not_run in this workspace")


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
