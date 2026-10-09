"""C07 — gateway identity configuration, read from the environment.

Two rules shape this module.

**No default provider.** There is no fallback issuer, no "development" realm
and no bundled client secret. A gateway that cannot name its identity provider
refuses to start. A gateway that starts with a guessed issuer would accept a
token from whoever guessed the same thing.

**Secrets are secrets in the representation too.** :class:`AuthSettings` is
frozen and has a ``__repr__`` that prints every field except the three that
carry a credential. Settings objects end up in tracebacks, in ``--reload``
logs and in support notes; a ``repr`` that prints a client secret undoes the
environment file it came from.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

UTC: Final = dt.UTC

#: Placeholders are legal values only inside ``deploy/identity/*.example``.
#: Anything carrying this prefix is a template, not a credential.
PLACEHOLDER_PREFIX: Final[str] = "__"

_REDACTED: Final[str] = "<redacted>"


class IdentityNotConfigured(RuntimeError):
    """Required identity configuration is missing or still a placeholder."""


def _required(env: Mapping[str, str], name: str) -> str:
    raw = env.get(name, "").strip()
    if not raw:
        raise IdentityNotConfigured(f"{name} is required")
    if raw.startswith(PLACEHOLDER_PREFIX):
        # A rendered template that was never substituted would otherwise
        # produce a gateway that starts and then fails every login, with the
        # real reason buried in a redirect.
        raise IdentityNotConfigured(f"{name} is still a template placeholder")
    return raw


def _optional(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name, "").strip()
    if not raw or raw.startswith(PLACEHOLDER_PREFIX):
        return None
    return raw


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise IdentityNotConfigured(f"{name} must be an integer") from exc
    if value < minimum:
        raise IdentityNotConfigured(f"{name} must be >= {minimum}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise IdentityNotConfigured(f"{name} must be a boolean")


@dataclass(frozen=True)
class AuthSettings:
    issuer: str
    audience: str
    client_id: str
    client_secret: str
    identity_key: str
    redirect_uri: str
    scopes: str = "openid profile email"
    discovery_url: str | None = None
    jwks_url: str | None = None
    post_logout_redirect_uri: str | None = None
    required_scopes: frozenset[str] = field(default_factory=frozenset)
    clock_skew_seconds: int = 30
    login_ttl_minutes: int = 10
    session_idle_minutes: int = 30
    session_absolute_hours: int = 12
    cookie_secure: bool = True
    http_timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AuthSettings:
        source = os.environ if env is None else env
        raw_scopes = _optional(source, "KB_OIDC_REQUIRED_SCOPES")
        return cls(
            issuer=_required(source, "KB_OIDC_ISSUER").rstrip("/"),
            audience=_required(source, "KB_OIDC_AUDIENCE"),
            client_id=_required(source, "KB_OIDC_CLIENT_ID"),
            client_secret=_required(source, "KB_OIDC_CLIENT_SECRET"),
            identity_key=_required(source, "KB_IDENTITY_KEY"),
            redirect_uri=_required(source, "KB_OIDC_REDIRECT_URI"),
            scopes=_optional(source, "KB_OIDC_SCOPES") or "openid profile email",
            discovery_url=_optional(source, "KB_OIDC_DISCOVERY_URL"),
            jwks_url=_optional(source, "KB_OIDC_JWKS_URL"),
            post_logout_redirect_uri=_optional(source, "KB_OIDC_POST_LOGOUT_REDIRECT_URI"),
            required_scopes=frozenset(raw_scopes.split()) if raw_scopes else frozenset(),
            clock_skew_seconds=_int(source, "KB_OIDC_CLOCK_SKEW_SECONDS", 30, minimum=0),
            login_ttl_minutes=_int(source, "KB_SESSION_LOGIN_TTL_MINUTES", 10),
            session_idle_minutes=_int(source, "KB_SESSION_IDLE_MINUTES", 30),
            session_absolute_hours=_int(source, "KB_SESSION_ABSOLUTE_HOURS", 12),
            cookie_secure=_bool(source, "KB_SESSION_COOKIE_SECURE", True),
            http_timeout_seconds=float(_optional(source, "KB_OIDC_HTTP_TIMEOUT") or 5.0),
        )

    def __repr__(self) -> str:
        shown = ", ".join(
            f"{key}={_REDACTED if key in _SECRET_FIELDS else value!r}"
            for key, value in sorted(self.__dict__.items())
            if key not in _HIDDEN_FIELDS
        )
        return f"AuthSettings({shown})"

    __str__ = __repr__


_SECRET_FIELDS = frozenset({"client_secret", "identity_key"})
#: printed as a presence marker instead of a value: the operator needs to know
#: whether a public URL was configured, not to read it out of a crash report.
_HIDDEN_FIELDS = frozenset({"discovery_url", "jwks_url", "post_logout_redirect_uri"})


def safe_return_target(raw: str | None) -> str | None:
    """A same-origin path to return to after login, or ``None``.

    ``None`` means "nothing was supplied", which is a legitimate answer and
    sends the browser to the application root. It is not the string "None",
    and it is not a guess at a previous page.

    Only an absolute *path* is accepted. ``//evil.example`` and
    ``https://evil.example`` are both refused: both would take the browser,
    and the ``__Host-`` cookie, to another origin.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or not candidate.startswith("/"):
        return None
    if candidate.startswith("//") or "\\" in candidate:
        return None
    return candidate
