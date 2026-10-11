"""Validate signed OIDC logout events using the existing provider JWKS and PyJWT."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import jwt

from kb.access.identity import (
    ALLOWED_ALGORITHMS,
    MAX_TOKEN_CHARS,
    IdentityRejected,
    IdentitySettings,
    TokenVerifier,
)


def decode_logout_token(
    token: str,
    settings: IdentitySettings,
    verifier: TokenVerifier,
) -> dict[str, Any]:
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise IdentityRejected("malformed_logout_token")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise IdentityRejected("malformed_logout_token") from None

    if header.get("alg") not in ALLOWED_ALGORITHMS:
        raise IdentityRejected("malformed_logout_token")
    if header.get("typ") not in (None, "JWT", "logout+jwt"):
        raise IdentityRejected("malformed_logout_token")
    if header.get("crit"):
        raise IdentityRejected("malformed_logout_token")

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise IdentityRejected("malformed_logout_token")
    key = verifier.jwks().key_for(kid)

    try:
        claims = jwt.decode(
            token,
            key=key.key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=settings.client_id,
            issuer=settings.issuer,
            leeway=settings.clock_skew_seconds,
            options={"require": ["iss", "aud", "iat", "exp", "jti"]},
        )
    except jwt.InvalidIssuerError:
        raise IdentityRejected("issuer_mismatch") from None
    except jwt.InvalidAudienceError:
        raise IdentityRejected("audience_mismatch") from None
    except jwt.ExpiredSignatureError:
        raise IdentityRejected("expired") from None
    except jwt.ImmatureSignatureError:
        raise IdentityRejected("not_yet_valid") from None
    except jwt.MissingRequiredClaimError:
        raise IdentityRejected("missing_claim") from None
    except jwt.PyJWTError:
        raise IdentityRejected("signature_invalid") from None
    except (TypeError, ValueError):
        raise IdentityRejected("malformed_logout_token") from None

    if not isinstance(claims, dict):
        raise IdentityRejected("malformed_logout_token")
    return claims


@dataclass(frozen=True)
class VerifiedLogout:
    issuer: str
    audience: str
    session_id: str
    subject: str | None
    jti: str
    issued_at: dt.datetime
    expires_at: dt.datetime


class LogoutVerifier:
    def __init__(
        self, settings: IdentitySettings, verifier: TokenVerifier, max_age_seconds: int = 300
    ) -> None:
        if max_age_seconds < 1:
            raise ValueError("logout token age must be positive")
        self._settings = settings
        self._verifier = verifier
        self._max_age = max_age_seconds

    def verify(self, token: str, *, now: dt.datetime) -> VerifiedLogout:
        claims = decode_logout_token(token, self._settings, self._verifier)
        issued, expires = claims["iat"], claims["exp"]
        if type(issued) is not int or type(expires) is not int:
            raise IdentityRejected("invalid_logout_time")
        moment = now.timestamp()
        skew = self._settings.clock_skew_seconds
        if expires <= issued or issued > moment + skew or expires <= moment - skew:
            raise IdentityRejected("invalid_logout_time")
        if issued < moment - self._max_age - skew:
            raise IdentityRejected("logout_token_too_old")
        events = claims.get("events")
        event = "http://schemas.openid.net/event/backchannel-logout"
        # Our Keycloak profile uses an empty event object (OIDC recommends it).
        if not isinstance(events, dict) or events.get(event) != {}:
            raise IdentityRejected("invalid_logout_event")
        if "nonce" in claims:
            raise IdentityRejected("logout_nonce_forbidden")
        sid = claims.get("sid")
        if not isinstance(sid, str) or not sid or len(sid) > 256:
            raise IdentityRejected("missing_logout_session")
        subject = claims.get("sub")
        if "sub" in claims and (not isinstance(subject, str) or not subject or len(subject) > 256):
            raise IdentityRejected("invalid_logout_subject")
        jti = claims["jti"]
        if not isinstance(jti, str) or not jti or len(jti) > 256:
            raise IdentityRejected("invalid_logout_jti")
        try:
            issued_at = dt.datetime.fromtimestamp(issued, dt.UTC)
            expires_at = dt.datetime.fromtimestamp(expires, dt.UTC)
        except (OverflowError, OSError, ValueError):
            raise IdentityRejected("invalid_logout_time") from None
        return VerifiedLogout(
            issuer=self._settings.issuer,
            audience=self._settings.client_id,
            session_id=sid,
            subject=subject,
            jti=jti,
            issued_at=issued_at,
            expires_at=expires_at,
        )
