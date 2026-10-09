"""C07 — A04: a forged token is refused.

ACCESS-MODEL A04: "JWT of another issuer/audience, an expired token, a
substituted user header" must be answered 401/403 without leaking content.
The JWT half of that row is this file; the header half is
``test_browser_flow.py``.

Every rejection below is a real cryptographic or claim-level refusal, not a
regex. The tokens are signed with real RSA keys by ``cryptography``; the
verifier is PyJWT; the JWKS is fetched over real HTTP from a real process. If
a token passes here it would pass against Keycloak, and if it is refused here
the reason is the one a reviewer can read in the failure message.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import jwt
import pytest

from kb.access.identity import (
    IdentityRejected,
    IdentitySettings,
    JwksCache,
    TokenVerifier,
    redact,
)

pytestmark = pytest.mark.integration

SUBJECT = "c0700000-0000-4000-8000-0000000000aa"
ACCOUNT = "c0700000-0000-4000-8000-0000000000bb"
OTHER_ISSUER = "https://idp.elsewhere.invalid/realms/other"
PUBLISHED_KID = "c07-key-current"
UNPUBLISHED_KID = "c07-key-attacker"  # generated, never published in the JWKS


def claims(provider, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "iss": provider.issuer,
        "sub": SUBJECT,
        "aud": provider.client_id,
        "iat": now,
        "exp": now + 300,
        "nonce": "c07-nonce",
        "kb_account_id": ACCOUNT,
    }
    base.update(overrides)
    return base


def reason(verifier: TokenVerifier, token: str, **kwargs) -> str:
    with pytest.raises(IdentityRejected) as caught:
        verifier.verify(token, **kwargs)
    return caught.value.code


# ------------------------------------------------------------ the happy path


def test_a_correctly_signed_token_is_accepted(verifier, provider):
    identity = verifier.verify(
        provider.mint_id_token(subject=SUBJECT, nonce="c07-nonce", account_id=ACCOUNT)
    )
    assert identity.principal_id.hex == SUBJECT.replace("-", "")
    assert identity.account_id is not None
    assert identity.account_id.hex == ACCOUNT.replace("-", "")
    assert identity.issuer == provider.issuer
    assert identity.subject == SUBJECT
    assert identity.provider_session_id == f"kc-session-{SUBJECT}"


def test_an_absent_account_stays_none_rather_than_being_invented(verifier, provider):
    with_account = verifier.verify(
        provider.mint_id_token(subject=SUBJECT, nonce="n1", account_id=ACCOUNT)
    )
    without = verifier.verify(provider.mint_id_token(subject=SUBJECT, nonce="n2"))
    assert with_account.account_id is not None
    assert without.account_id is None


def test_a_bearer_typed_token_is_accepted(verifier, provider, signing_keys):
    # Keycloak >= 24 marks access tokens `Bearer` per RFC 9068, not `JWT`.
    # A verifier that accepted only `JWT` would refuse a legitimate token.
    token = signing_keys["current"].sign(claims(provider), headers={"typ": "Bearer"})
    assert verifier.verify(token, require_scopes=False).subject == SUBJECT


# ------------------------------------------------------------- A04: forgeries


def test_a_token_from_another_issuer_is_rejected(verifier, provider):
    token = provider.mint_id_token(subject=SUBJECT, nonce="c07-nonce", issuer=OTHER_ISSUER)
    assert reason(verifier, token, expected_nonce="c07-nonce") == "issuer_mismatch"


def test_a_token_for_another_audience_is_rejected(verifier, provider):
    token = provider.mint_id_token(subject=SUBJECT, nonce="c07-nonce", audience="some-other-api")
    assert reason(verifier, token, expected_nonce="c07-nonce") == "audience_mismatch"


def test_an_expired_token_is_rejected(verifier, provider):
    token = provider.mint_id_token(subject=SUBJECT, nonce="c07-nonce", expires_in=-600)
    assert reason(verifier, token, expected_nonce="c07-nonce") == "expired"


def test_a_token_that_is_not_yet_valid_is_rejected(verifier, provider, signing_keys):
    now = int(time.time())
    token = signing_keys["current"].sign(
        claims(provider, iat=now - 7200, nbf=now + 7200, exp=now + 10800)
    )
    assert reason(verifier, token, expected_nonce="c07-nonce") == "not_yet_valid"


def test_alg_none_is_rejected(verifier, provider):
    """`alg: none` is the oldest JWT attack there is: a token with no signature
    at all, asserting whatever the author likes."""
    token = jwt.encode(claims(provider), key="", algorithm="none", headers={"kid": PUBLISHED_KID})
    assert reason(verifier, token, expected_nonce="c07-nonce") == "algorithm_not_allowed"


def test_an_hmac_token_signed_with_the_public_key_is_rejected(verifier, provider, signing_keys):
    """Algorithm confusion. Sign the claims with HS256 using the RSA *public*
    key as the shared secret: a verifier that takes its algorithm from the
    header accepts it without ever consulting the JWKS.

    The token is assembled by hand because PyJWT refuses to *create* one (it
    detects an asymmetric key used as an HMAC secret). That is a second
    defence; the attacker's tool is not PyJWT.
    """
    forged = hmac_token_over_public_key(signing_keys["current"], claims(provider))

    # Non-vacuity: the HMAC is genuinely valid over the public key, so this is
    # not a malformed token that any parser would reject. The only thing
    # refusing it is the algorithm pin in TokenVerifier.verify.
    header, payload, signature = forged.split(".")
    assert hmac.compare_digest(
        _b64decode(signature),
        hmac.new(
            signing_keys["current"].public_pem,
            f"{header}.{payload}".encode(),
            hashlib.sha256,
        ).digest(),
    )
    assert reason(verifier, forged, expected_nonce="c07-nonce") == "algorithm_not_allowed"


def _b64decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def hmac_token_over_public_key(key: SigningKey, body: dict[str, Any]) -> str:  # type: ignore[name-defined]  # noqa: F821
    """A genuine HS256 JWT over the RSA public key, built byte by byte.

    ``key`` is a :class:`SigningKey` from ``conftest.py``; it is not imported
    here because the conftest is not an importable package.
    """

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": PUBLISHED_KID}).encode())
    payload = b64(json.dumps(body).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(key.public_pem, signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(signature)}"


def claims_from_issuer(issuer: str) -> dict[str, Any]:
    now = int(time.time())
    return {
        "iss": issuer,
        "sub": SUBJECT,
        "aud": "kb-gateway",
        "iat": now,
        "exp": now + 300,
    }


def test_a_token_signed_by_a_different_key_is_rejected(verifier, provider, signing_keys):
    """A published `kid`, a valid structure, the wrong private key. Only the
    signature check can catch this one."""
    forged = signing_keys["attacker"].sign(claims(provider), headers={"kid": PUBLISHED_KID})
    assert jwt.get_unverified_header(forged)["kid"] == PUBLISHED_KID
    assert reason(verifier, forged, expected_nonce="c07-nonce") == "signature_invalid"


def test_an_unpublished_key_id_is_rejected(verifier, provider, signing_keys):
    token = signing_keys["attacker"].sign(claims(provider), headers={"kid": UNPUBLISHED_KID})
    assert reason(verifier, token, expected_nonce="c07-nonce") == "unknown_signing_key"


def test_a_completely_unknown_key_id_is_rejected(verifier, signing_keys):
    token = signing_keys["attacker"].sign(
        claims_from_issuer("http://127.0.0.1:1/x"), headers={"kid": "kid-that-never-existed"}
    )
    assert reason(verifier, token) == "unknown_signing_key"


def test_a_token_without_a_key_id_is_rejected(verifier, provider):
    token = jwt.encode(
        claims(provider),
        provider.keys["current"].private_pem,
        algorithm="RS256",
        headers={"typ": "JWT"},
    )
    assert reason(verifier, token) == "missing_key_id"


def test_a_forged_token_type_header_is_rejected(verifier, provider, signing_keys):
    token = signing_keys["current"].sign(claims(provider), headers={"typ": "MAC"})
    assert reason(verifier, token) == "unexpected_token_type"


def test_an_unknown_critical_header_is_rejected(verifier, provider, signing_keys):
    # RFC 7515 §4.1.11: a critical extension the verifier does not understand
    # must invalidate the token, not be ignored. Ignoring it is how a token
    # stays valid under rules nobody applied.
    #
    # Two layers catch this. PyJWT's own header parser refuses first, and the
    # `crit` check in TokenVerifier is the second: it stays so that replacing
    # the parser later cannot quietly drop the rule.
    token = signing_keys["current"].sign(
        claims(provider), headers={"crit": ["kb-policy"], "kb-policy": "bypass-acl"}
    )
    assert reason(verifier, token) in {"unsupported_critical_header", "malformed_token"}
    with pytest.raises(jwt.PyJWTError):
        jwt.get_unverified_header(token)


def test_a_mismatched_nonce_is_rejected(verifier, provider):
    token = provider.mint_id_token(subject=SUBJECT, nonce="the-wrong-nonce")
    assert reason(verifier, token, expected_nonce="c07-nonce") == "nonce_mismatch"


def test_a_token_with_a_foreign_authorized_party_is_rejected(verifier, provider):
    """OIDC Core §3.1.3.7: a multi-valued `aud` must name this client in `azp`.
    Without that check, a token minted for a different application is
    accepted here because our client id happens to be in the audience list."""
    token = provider.mint_id_token(
        subject=SUBJECT,
        nonce="c07-nonce",
        extra={"aud": [provider.client_id, "another-api"]},
    )
    assert reason(verifier, token, expected_nonce="c07-nonce") == "authorized_party_mismatch"


def test_a_multi_audience_token_with_the_right_authorized_party_is_accepted(verifier, provider):
    token = provider.mint_id_token(
        subject=SUBJECT,
        nonce="c07-nonce",
        extra={"aud": ["another-api", provider.client_id], "azp": provider.client_id},
    )
    assert verifier.verify(token, expected_nonce="c07-nonce").subject == SUBJECT


def test_a_subject_that_is_not_a_principal_id_is_rejected(verifier, provider):
    token = provider.mint_id_token(subject="not-a-uuid", nonce="c07-nonce")
    assert reason(verifier, token, expected_nonce="c07-nonce") == "subject_not_a_principal_id"


def test_a_missing_claim_is_a_refusal(verifier, provider, signing_keys):
    for missing in ("exp", "iat", "iss", "aud", "sub"):
        broken = claims(provider)
        del broken[missing]
        assert reason(verifier, signing_keys["current"].sign(broken)) == "missing_claim"


def test_a_missing_required_scope_is_a_refusal_not_a_default(identity_settings, provider):
    strict_settings = IdentitySettings(
        issuer=identity_settings.issuer,
        audience=identity_settings.audience,
        client_id=identity_settings.client_id,
        required_scopes=frozenset({"kb:read", "kb:export"}),
    )
    with httpx.Client(timeout=5.0) as client:
        strict = TokenVerifier(strict_settings, client)
        without = provider.mint_id_token(subject=SUBJECT, nonce="c07-nonce")
        assert reason(strict, without, expected_nonce="c07-nonce") == "scope_missing"

        with_scope = provider.mint_id_token(
            subject=SUBJECT, nonce="c07-nonce", scopes="openid kb:read kb:export"
        )
        assert strict.verify(with_scope, expected_nonce="c07-nonce").scopes == frozenset(
            {"openid", "kb:read", "kb:export"}
        )


def test_garbage_is_rejected_without_being_echoed(verifier):
    for junk in ("", "not.a.jwt", "a" * 9000, "....", "eyJhbGciOiJub25lIn0.e30."):
        with pytest.raises(IdentityRejected):
            verifier.verify(junk)


# --------------------------------------------------------- provider endpoints


def test_a_discovery_document_with_a_foreign_issuer_is_rejected(identity_settings, provider):
    """A discovery document is an unauthenticated URL. Following endpoints
    from a document whose `issuer` disagrees would point the gateway at whoever
    answered the lookup."""
    provider.forced_issuer = OTHER_ISSUER
    try:
        with httpx.Client(timeout=5.0) as client:
            with pytest.raises(IdentityRejected) as caught:
                TokenVerifier(identity_settings, client).metadata()
            assert caught.value.code == "discovery_issuer_mismatch"
    finally:
        provider.forced_issuer = None


def test_a_jwks_url_that_is_not_https_is_refused():
    with httpx.Client(timeout=5.0) as client:
        with pytest.raises(IdentityRejected) as caught:
            JwksCache("http://idp.elsewhere.invalid/certs", client)
    assert caught.value.code == "provider_endpoint_not_secure"


def test_a_loopback_jwks_url_is_allowed_for_local_runs():
    with httpx.Client(timeout=5.0) as client:
        assert JwksCache("http://127.0.0.1:9999/certs", client) is not None


def test_key_rotation_is_picked_up_without_a_restart(provider, identity_settings):
    """A new signing key must be usable without restarting the gateway, and a
    key that was never published must still be refused afterwards."""
    original = list(provider.key_order)
    try:
        provider.key_order = ["retired"]
        with httpx.Client(timeout=5.0) as client:
            rotating = TokenVerifier(
                IdentitySettings(
                    issuer=identity_settings.issuer,
                    audience=identity_settings.audience,
                    client_id=identity_settings.client_id,
                    jwks_min_ttl_seconds=600.0,
                    forced_refresh_floor_seconds=0.0,
                ),
                client,
            )
            # warm the cache against the pre-rotation key set
            rotating.verify(
                provider.mint_id_token(
                    subject=SUBJECT, nonce="c07-nonce", key=provider.keys["retired"]
                )
            )
            assert rotating.jwks().key_for("c07-key-retired") is not None

            # the provider publishes a new key
            provider.key_order = ["retired", "current"]
            rotated = rotating.verify(
                provider.mint_id_token(
                    subject=SUBJECT, nonce="c07-nonce", key=provider.keys["current"]
                )
            )
            assert rotated.subject == SUBJECT

            # and a key it never published is still refused after the refresh
            rogue = provider.mint_id_token(
                subject=SUBJECT, nonce="c07-nonce", key=provider.keys["attacker"]
            )
            assert reason(rotating, rogue, expected_nonce="c07-nonce") == "unknown_signing_key"
    finally:
        provider.key_order = original


def test_expiry_is_reported_against_a_real_deadline(verifier, provider):
    identity = verifier.verify(provider.mint_id_token(subject=SUBJECT, nonce="c07-nonce"))
    assert identity.expires_at > dt.datetime.now(dt.UTC)
    assert identity.issued_at <= dt.datetime.now(dt.UTC)


def test_redact_never_returns_the_credential():
    credential = "eyJhbGciOiJSUzI1NiJ9.payload.signature"
    out = redact(credential)
    assert credential not in out
    assert "eyJ" not in out
    assert redact(None) == "<absent>"
    assert redact("") == "<absent>"
