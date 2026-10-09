"""C07 — verification of identity-provider tokens.

This module *verifies*. It does not authenticate anybody and it does not
implement OAuth: the authorization server is Keycloak, and everything that
decides who a user is stays there. What lives here is the relying-party half —
checking a token that Keycloak minted before the gateway believes the claims
inside it.

Library choice, and why
-----------------------
**PyJWT** (`jwt`, pinned in ``requirements.lock`` and already installed as a
dependency of the official MCP SDK) does the cryptography and the claim
checks: RSA signature verification, JWK→key conversion, ``iss``/``aud``/
``exp``/``nbf``/``iat`` validation and — the part that actually matters —
algorithm pinning through ``algorithms=[...]``, which is what makes an
``alg: none`` or ``HS256``-signed-with-the-public-key token impossible.

**httpx** (a declared project dependency) does the OIDC discovery fetch and
the JWKS fetch. Both are plain HTTPS GETs of documents the provider
publishes; there is nothing for a heavier library to add, and keeping them on
the same HTTP stack as the rest of the gateway means one connection pool and
one timeout policy.

The obvious alternative is **Authlib**, which would additionally own the
authorization-code exchange and the PKCE bookkeeping. It is not used here for
one concrete reason: it is not in ``requirements.lock`` or ``pyproject.toml``,
and this card is not allowed to edit either file — AGENTS.md names a single
owner for lockfiles. Installing an undeclared package into the venv would
make this tree pass locally and break for every other executor, which is
exactly the kind of green that transfers a false claim. The proposal for the
lockfile owner is recorded in ``docs/handoff/results/C07.json``; the swap is
contained in :mod:`kb.http.auth_flow`, which is the only module that talks to
the token endpoint.

What is NOT reimplemented here: RSA, SHA-256, JWK parsing, base64url, and
every JWT claim check. The glue that remains is deliberately thin.

Identity comes from here or it does not exist. Nothing in this module reads a
caller-supplied ``user_id``, an ``X-User-Id`` header or a request body, and
the reasons below are the ones a forged token can actually produce.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import httpx
import jwt
from jwt import PyJWK, PyJWKSet

#: RFC 7518 asymmetric algorithms only. An entry here is an algorithm this
#: service will ever verify. `none`, HS* and every symmetric algorithm are
#: absent by construction, not by a runtime filter.
ALLOWED_ALGORITHMS: Final[tuple[str, ...]] = ("RS256",)

#: `typ` values that may appear on a JWT. Keycloak marks ID tokens `JWT` and,
#: since v24, access tokens `Bearer` per RFC 9068. Anything else is a token
#: shaped for a different protocol.
ALLOWED_TYP: Final[frozenset[str]] = frozenset({"JWT", "at+jwt", "Bearer"})

#: A JWT longer than this is a denial-of-service, not a token.
MAX_TOKEN_CHARS: Final[int] = 8192

#: Hosts for which plain http is tolerated. A JWKS document fetched over
#: cleartext is a document an attacker chooses, so the exemption is limited to
#: the loopback interface — which is where the integration tests and a local
#: development Keycloak live.
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

#: Minimum delay between two forced JWKS refreshes. Without it, a caller who
#: invents a fresh `kid` on every request turns token verification into an
#: outbound request amplifier pointed at the identity provider.
FORCED_REFRESH_FLOOR_SECONDS: Final[float] = 5.0

UTC: Final = dt.UTC


class IdentityRejected(Exception):
    """A token was refused.

    ``code`` is a fixed vocabulary, never the token and never an upstream
    error string: this exception reaches an HTTP response and a log line, and
    both of those are places where a provider's error body could otherwise
    echo the credential back out.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class IdentitySettings:
    """Provider coordinates. No defaults: a missing issuer is a startup error.

    ``jwks_url`` may be left empty, in which case it is discovered from
    ``{issuer}/.well-known/openid-configuration`` at first use.
    """

    issuer: str
    audience: str
    client_id: str
    jwks_url: str | None = None
    discovery_url: str | None = None
    required_scopes: frozenset[str] = frozenset()
    clock_skew_seconds: int = 30
    jwks_min_ttl_seconds: float = 60.0
    jwks_max_ttl_seconds: float = 3600.0
    #: Overridable only so the rotation test can be deterministic; production
    #: keeps the 5 s floor that stops a `kid` flood from becoming a request
    #: amplifier aimed at the provider.
    forced_refresh_floor_seconds: float = FORCED_REFRESH_FLOOR_SECONDS
    http_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience or not self.client_id:
            raise ValueError("issuer, audience and client_id are required; there is no default IdP")
        if not self.issuer.startswith("https://") and not _is_loopback(self.issuer):
            raise ValueError("issuer must be https")


@dataclass(frozen=True)
class VerifiedIdentity:
    """What a token is allowed to assert, after verification.

    ``principal_id`` is the address used by ``kb.library_grant``. ``account_id``
    stays ``None`` when the provider does not carry one: unknown is ``None``,
    never a fabricated default, and a downstream check that needs an account
    must fail closed rather than receive a placeholder.
    """

    issuer: str
    subject: str
    principal_id: UUID
    account_id: UUID | None
    scopes: frozenset[str]
    issued_at: dt.datetime
    expires_at: dt.datetime
    provider_session_id: str | None = None
    email: str | None = None

    @property
    def principal_key(self) -> tuple[str, str]:
        """The (issuer, subject) pair the access model is written against."""
        return (self.issuer, self.subject)


def _is_loopback(url: str) -> bool:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    return host in LOOPBACK_HOSTS


def _require_secure_transport(url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and (parts.hostname or "").lower() in LOOPBACK_HOSTS:
        return
    raise IdentityRejected("provider_endpoint_not_secure")


# ------------------------------------------------------------------ JWKS


class JwksCache:
    """Signing keys, fetched over HTTPS and held for a bounded time.

    Key rotation is a normal event, not an outage: an unknown ``kid`` forces
    one refresh, rate-limited by :data:`FORCED_REFRESH_FLOOR_SECONDS`.
    """

    def __init__(
        self,
        url: str,
        client: httpx.Client,
        *,
        min_ttl: float = 60.0,
        max_ttl: float = 3600.0,
        forced_refresh_floor: float = FORCED_REFRESH_FLOOR_SECONDS,
    ) -> None:
        _require_secure_transport(url)
        self._url = url
        self._client = client
        self._min_ttl = min_ttl
        self._max_ttl = max_ttl
        self._forced_refresh_floor = forced_refresh_floor
        self._keys: dict[str, PyJWK] = {}
        self._expires_at: float = 0.0
        self._last_forced: float = 0.0

    def _ttl_from_response(self, response: httpx.Response) -> float:
        header = response.headers.get("cache-control", "")
        for part in header.split(","):
            name, _, value = part.strip().partition("=")
            if name.lower() == "max-age":
                try:
                    return max(self._min_ttl, min(self._max_ttl, float(value)))
                except ValueError:
                    break
        return self._min_ttl

    def _refresh(self) -> None:
        try:
            response = self._client.get(self._url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IdentityRejected("jwks_unavailable") from exc
        try:
            key_set = PyJWKSet.from_dict(document)
            keys = {jwk.key_id: jwk for jwk in key_set.keys if jwk.key_id}
        except (jwt.PyJWKError, AttributeError, TypeError) as exc:
            raise IdentityRejected("jwks_malformed") from exc
        if not keys:
            raise IdentityRejected("jwks_empty")
        self._keys = keys
        self._expires_at = time.monotonic() + self._ttl_from_response(response)

    def key_for(self, kid: str) -> PyJWK:
        now = time.monotonic()
        if now >= self._expires_at or kid not in self._keys:
            if now - self._last_forced < self._forced_refresh_floor and self._keys:
                if kid not in self._keys:
                    raise IdentityRejected("unknown_signing_key")
            self._last_forced = now
            self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise IdentityRejected("unknown_signing_key")
        return key


# --------------------------------------------------------------- verifier


def _header(token: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise IdentityRejected("malformed_token") from exc
    if not isinstance(header, dict):
        raise IdentityRejected("malformed_token")
    return header


def _scopes_of(claims: dict[str, Any]) -> frozenset[str]:
    """Read scopes from either the OAuth2 string or the OIDC array form.

    An absent scope claim is an empty set, not a guess. The required-scope
    check on the verifier is what turns that absence into a refusal.
    """
    scope = claims.get("scope")
    if isinstance(scope, str):
        return frozenset(part for part in scope.split() if part)
    scp = claims.get("scp")
    if isinstance(scp, list):
        return frozenset(str(item) for item in scp)
    return frozenset()


def _as_utc(value: Any) -> dt.datetime:
    if not isinstance(value, (int, float)):
        raise IdentityRejected("malformed_token")
    return dt.datetime.fromtimestamp(float(value), UTC)


def _uuid_or_none(value: Any) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


class TokenVerifier:
    """Verifies ID and access tokens against one configured provider."""

    def __init__(
        self,
        settings: IdentitySettings,
        client: httpx.Client,
        *,
        jwks: JwksCache | None = None,
        discovery: dict[str, Any] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._discovery = discovery
        self._jwks = jwks

    # -- discovery ------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        """The provider's discovery document, issuer-checked.

        A discovery document is an unauthenticated URL. If its ``issuer``
        disagrees with the configured one, following its endpoints would point
        the gateway at whoever answered the lookup, so the document is
        rejected instead of trusted.
        """
        if self._discovery is not None:
            return self._discovery
        url = self._settings.discovery_url or (
            f"{self._settings.issuer.rstrip('/')}/.well-known/openid-configuration"
        )
        _require_secure_transport(url)
        try:
            response = self._client.get(url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IdentityRejected("discovery_unavailable") from exc
        if not isinstance(document, dict):
            raise IdentityRejected("discovery_malformed")
        if document.get("issuer") != self._settings.issuer:
            raise IdentityRejected("discovery_issuer_mismatch")
        self._discovery = document
        return document

    def endpoint(self, name: str) -> str:
        url = self.metadata().get(name)
        if not isinstance(url, str) or not url:
            raise IdentityRejected(f"discovery_missing_{name}")
        _require_secure_transport(url)
        return url

    def jwks(self) -> JwksCache:
        if self._jwks is None:
            url = self._settings.jwks_url or self.endpoint("jwks_uri")
            self._jwks = JwksCache(
                url,
                self._client,
                min_ttl=self._settings.jwks_min_ttl_seconds,
                max_ttl=self._settings.jwks_max_ttl_seconds,
                forced_refresh_floor=self._settings.forced_refresh_floor_seconds,
            )
        return self._jwks

    # -- verification ---------------------------------------------------

    def verify(
        self,
        token: str,
        *,
        expected_nonce: str | None = None,
        require_scopes: bool = True,
    ) -> VerifiedIdentity:
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise IdentityRejected("malformed_token")

        header = _header(token)
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise IdentityRejected("algorithm_not_allowed")
        typ = header.get("typ")
        if typ is not None and typ not in ALLOWED_TYP:
            raise IdentityRejected("unexpected_token_type")
        # RFC 7515 §4.1.11: an unrecognised critical extension must be
        # rejected, not ignored. Ignoring it is how a token stays valid under
        # rules the verifier never applied.
        if header.get("crit"):
            raise IdentityRejected("unsupported_critical_header")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise IdentityRejected("missing_key_id")

        key = self.jwks().key_for(kid)

        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=self._settings.clock_skew_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.InvalidIssuerError as exc:
            raise IdentityRejected("issuer_mismatch") from exc
        except jwt.InvalidAudienceError as exc:
            raise IdentityRejected("audience_mismatch") from exc
        except jwt.ExpiredSignatureError as exc:
            raise IdentityRejected("expired") from exc
        except jwt.ImmatureSignatureError as exc:
            raise IdentityRejected("not_yet_valid") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise IdentityRejected("missing_claim") from exc
        except jwt.PyJWTError as exc:
            raise IdentityRejected("signature_invalid") from exc

        if not isinstance(claims, dict):
            raise IdentityRejected("malformed_token")

        # OIDC Core §3.1.3.7: when `aud` holds more than one value, the
        # authorized party must be this client. A token minted for somebody
        # else, replayed here with our audience added, fails here.
        audience = claims.get("aud")
        if isinstance(audience, list) and len(audience) > 1:
            if claims.get("azp") != self._settings.client_id:
                raise IdentityRejected("authorized_party_mismatch")

        if expected_nonce is not None and claims.get("nonce") != expected_nonce:
            raise IdentityRejected("nonce_mismatch")

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise IdentityRejected("missing_subject")
        # A grant row is addressed by UUID. A subject that is not one cannot
        # own a grant, and inventing a UUID from it would grant access to a
        # person the database has never heard of.
        principal_id = _uuid_or_none(subject)
        if principal_id is None:
            raise IdentityRejected("subject_not_a_principal_id")

        scopes = _scopes_of(claims)
        if require_scopes:
            missing = self._settings.required_scopes - scopes
            if missing:
                raise IdentityRejected("scope_missing")

        provider_session = claims.get("session_state") or claims.get("sid")
        expires_at = _as_utc(claims.get("exp"))
        if expires_at <= dt.datetime.now(UTC):
            raise IdentityRejected("expired")

        return VerifiedIdentity(
            issuer=claims["iss"],
            subject=subject,
            principal_id=principal_id,
            account_id=_uuid_or_none(claims.get("kb_account_id")),
            scopes=scopes,
            issued_at=_as_utc(claims.get("iat")),
            expires_at=expires_at,
            provider_session_id=(
                provider_session if isinstance(provider_session, str) and provider_session else None
            ),
            email=claims.get("email") if isinstance(claims.get("email"), str) else None,
        )


def redact(token: str | None) -> str:
    """A log-safe stand-in for a credential.

    Never the token, never a prefix of it: a prefix of a JWT is a prefix of a
    bearer credential and log storage outlives the token's usefulness.
    """
    if not token:
        return "<absent>"
    return f"<redacted len={len(token)}>"
