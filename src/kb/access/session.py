"""C07 — durable gateway sessions and the pending OAuth transaction.

Everything in this module lives in PostgreSQL, on purpose.

The failure this design exists to prevent: a session held in the memory of one
gateway process. It works until the process restarts, it works until a second
gateway is started for a rolling deploy, and then the user is silently logged
out — or worse, the second gateway accepts a session it never issued because
a shared cache said so. SCALING.md S01 requires that a browser alternates
between two gateways and that restarting one changes nothing. That holds only
if the state is in the database.

Two tables, both added by ``migrations/0003_identity_sessions.sql``:

* ``auth_login_transaction`` — the in-flight authorization request: the
  ``state`` digest, the sealed PKCE verifier, the redirect target. Surviving
  a restart is what lets a callback that arrives at gateway B complete a login
  that gateway A started.
* ``browser_session`` — the established session: identity, expiry, and the
  digests of the cookie secret and the CSRF token.

No access, refresh or ID token is stored. A session row is a credential, and
a credential stored next to an unexpired bearer token doubles what a stolen
database backup is worth for no benefit. The token is discarded in the memory
of the request that exchanged the code.

Secrets at rest are digests. The cookie carries ``<session uuid>.<256-bit
secret>``; the table keeps ``sha256(secret)``. A row therefore cannot be
converted back into a cookie, and a wrong cookie costs 2^256 to guess.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from psycopg import Connection
from psycopg.rows import dict_row

from kb.access.identity import VerifiedIdentity
from kb.access.policy import Principal

UTC: Final = dt.UTC

#: Cookie carrying the session. HttpOnly: script must not read it.
SESSION_COOKIE: Final[str] = "kb_session"
#: Cookie carrying the CSRF token. Deliberately NOT HttpOnly — the browser has
#: to read it to echo it in a header. That is safe only because the expected
#: value is also kept server-side as a digest; see `csrf_matches`.
CSRF_COOKIE: Final[str] = "kb_csrf"
#: Header the browser echoes the CSRF token in.
CSRF_HEADER: Final[str] = "x-kb-csrf"

#: Methods that change state and therefore require a CSRF token. A GET never
#: does.
CSRF_PROTECTED_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_SECRET_BYTES: Final[int] = 32
_CSRF_BYTES: Final[int] = 32
_STATE_BYTES: Final[int] = 32
_NONCE_BYTES: Final[int] = 16
#: RFC 7636 §4.1 allows 43..128 characters. 64 random bytes -> 86 base64url
#: characters, inside the window.
_VERIFIER_BYTES: Final[int] = 64


class SessionMissing(Exception):
    """No live session matches the presented cookie. Not an error to report."""


class SessionIdentityIncomplete(Exception):
    """The session exists but cannot be turned into a principal.

    Raised instead of filling a missing account with a placeholder. A principal
    with a fabricated account is a principal whose access was decided against
    a tenant that does not exist.
    """


class LoginTransactionInvalid(Exception):
    """Unknown, expired or already-consumed ``state``."""


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def new_state() -> str:
    return secrets.token_urlsafe(_STATE_BYTES)


def new_browser_binding() -> str:
    """Value for the short-lived cookie that ties a callback to its browser."""
    return secrets.token_urlsafe(_STATE_BYTES)


def new_nonce() -> str:
    return secrets.token_urlsafe(_NONCE_BYTES)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(_VERIFIER_BYTES)


def code_challenge_for(verifier: str) -> str:
    """RFC 7636 §4.2 ``S256``: BASE64URL(SHA256(ASCII(code_verifier))).

    This is one SHA-256 over a string, with the base64url alphabet applied to
    the digest. It is the whole of PKCE's mathematics; there is no protocol
    decision to get wrong beyond using ``S256`` rather than ``plain``, and
    ``plain`` is not offered anywhere in this codebase.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class Sealer:
    """Authenticated encryption for the in-flight PKCE verifier.

    Between ``/auth/login`` and ``/auth/callback`` the verifier is a live
    credential for a pending authorization code. It is stored sealed, and the
    key comes from the environment; there is no default key, because a
    default key is the same as no encryption.
    """

    def __init__(self, key: str | bytes) -> None:
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ValueError("KB_IDENTITY_KEY must be a valid Fernet key") from exc

    @classmethod
    def generate(cls) -> str:
        """A fresh key. For first-run provisioning, never for an import."""
        return Fernet.generate_key().decode()

    def seal(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def unseal(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SessionIdentityIncomplete("sealed_login_transaction_unreadable") from exc


@dataclass(frozen=True)
class LoginTransaction:
    id: UUID
    code_verifier: str
    code_challenge: str
    redirect_uri: str
    return_to: str | None
    nonce: str | None


@dataclass(frozen=True)
class Session:
    id: UUID
    principal_id: UUID
    account_id: UUID | None
    issuer: str
    subject: str
    csrf_token_hash: bytes
    created_at: dt.datetime
    last_seen_at: dt.datetime
    idle_expires_at: dt.datetime
    absolute_expires_at: dt.datetime

    def to_principal(self) -> Principal:
        """The C06 :class:`Principal` this session asserts.

        ``generation_watermark`` stays at C06's default of 1. The real
        watermark is a catalog concern (per-library generation) and inventing a
        larger one here would claim knowledge of data this module has not read.
        """
        if self.account_id is None:
            raise SessionIdentityIncomplete("session_has_no_account")
        return Principal(
            principal_id=self.principal_id,
            account_id=self.account_id,
            generation_watermark=1,
        )


@dataclass(frozen=True)
class IssuedSession:
    session: Session
    cookie_value: str
    csrf_token: str


class SessionStore:
    """PostgreSQL-backed session state. One instance == one gateway process.

    Two instances pointed at the same database share nothing but rows. That
    property is the point, and it is what the S01 test exercises.
    """

    def __init__(
        self,
        dsn: str,
        *,
        instance_id: str,
        sealer: Sealer,
        role: str = "kb_app",
        idle_minutes: int = 30,
        absolute_hours: int = 12,
        login_ttl_minutes: int = 10,
    ) -> None:
        if idle_minutes < 1 or absolute_hours < 1 or login_ttl_minutes < 1:
            raise ValueError("session lifetimes must be positive")
        self._dsn = dsn
        self.instance_id = instance_id
        self._sealer = sealer
        self._role = role
        self._idle = dt.timedelta(minutes=idle_minutes)
        self._absolute = dt.timedelta(hours=absolute_hours)
        self._login_ttl = dt.timedelta(minutes=login_ttl_minutes)

    # -- plumbing -------------------------------------------------------

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Connection[dict[str, Any]]]:
        conn: Connection[dict[str, Any]] = Connection.connect(
            self._dsn, autocommit=True, row_factory=dict_row
        )
        try:
            if self._role:
                # The gateway reads credential material as kb_app, the
                # unprivileged role. Running the store as the table owner
                # would prove nothing about the privilege model in 0003.
                # The interpolated name is one of two literals chosen by the
                # caller, never request input.
                conn.execute(f"SET ROLE {self._role}")
            yield conn
        finally:
            conn.close()

    # -- login transaction ----------------------------------------------

    def open_login(
        self,
        *,
        state: str,
        binding: str,
        code_verifier: str,
        code_challenge: str,
        redirect_uri: str,
        return_to: str | None,
        nonce: str | None,
        now: dt.datetime,
    ) -> UUID:
        """Record the in-flight authorization request. Durable from here on."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kb.auth_login_transaction (
                    state_hash, binding_hash, code_verifier_sealed, code_challenge,
                    redirect_uri, return_to, nonce, expires_at, created_by_instance
                ) VALUES (
                    %(state_hash)s, %(binding_hash)s, %(sealed)s, %(challenge)s,
                    %(redirect_uri)s, %(return_to)s, %(nonce)s, %(expires_at)s, %(instance)s
                ) RETURNING id
                """,
                {
                    "state_hash": _digest(state),
                    "binding_hash": _digest(binding),
                    "sealed": self._sealer.seal(code_verifier),
                    "challenge": code_challenge,
                    "redirect_uri": redirect_uri,
                    "return_to": return_to,
                    "nonce": nonce,
                    "expires_at": now + self._login_ttl,
                    "instance": self.instance_id,
                },
            )
            row = cur.fetchone()
        assert row is not None  # RETURNING always yields a row or raises
        return row["id"]

    def consume_login(
        self, state: str, *, binding: str | None, now: dt.datetime
    ) -> LoginTransaction:
        """Claim the pending login exactly once, in the browser that started it.

        The single ``UPDATE ... WHERE consumed_at IS NULL`` is the replay
        defence: a second callback carrying the same ``state`` matches no row,
        so a stolen authorization code cannot be redeemed twice. The
        ``binding_hash`` term is the login-CSRF defence: a callback delivered
        to somebody else's browser does not match either.
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb.auth_login_transaction
                   SET consumed_at = %(now)s
                 WHERE state_hash = %(state_hash)s
                   AND binding_hash = %(binding_hash)s
                   AND consumed_at IS NULL
                   AND %(now)s < expires_at
                RETURNING id, code_verifier_sealed, code_challenge, redirect_uri,
                          return_to, nonce
                """,
                {
                    "state_hash": _digest(state),
                    # An absent cookie hashes to a value no stored row holds,
                    # so a callback with no browser binding simply matches
                    # nothing. It is not special-cased into acceptance.
                    "binding_hash": _digest(binding or ""),
                    "now": now,
                },
            )
            row = cur.fetchone()
        if row is None:
            raise LoginTransactionInvalid("state_unknown_expired_or_used")
        return LoginTransaction(
            id=row["id"],
            code_verifier=self._sealer.unseal(row["code_verifier_sealed"]),
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            return_to=row["return_to"],
            nonce=row["nonce"],
        )

    # -- sessions -------------------------------------------------------

    def create_session(self, identity: VerifiedIdentity, *, now: dt.datetime) -> IssuedSession:
        session_id = uuid4()
        cookie_secret = secrets.token_urlsafe(_SECRET_BYTES)
        csrf_token = secrets.token_urlsafe(_CSRF_BYTES)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kb.browser_session (
                    id, secret_hash, csrf_token_hash, principal_id, account_id,
                    issuer, subject, provider_session_id,
                    idle_expires_at, absolute_expires_at, created_by_instance,
                    created_at, last_seen_at
                ) VALUES (
                    %(id)s, %(secret_hash)s, %(csrf_hash)s, %(principal_id)s, %(account_id)s,
                    %(issuer)s, %(subject)s, %(provider_session_id)s,
                    %(idle)s, %(absolute)s, %(instance)s, %(now)s, %(now)s
                )
                RETURNING id, created_at, idle_expires_at, absolute_expires_at
                """,
                {
                    "id": session_id,
                    "secret_hash": _digest(cookie_secret),
                    "csrf_hash": _digest(csrf_token),
                    "principal_id": identity.principal_id,
                    "account_id": identity.account_id,
                    "issuer": identity.issuer,
                    "subject": identity.subject,
                    "provider_session_id": identity.provider_session_id,
                    "idle": now + self._idle,
                    "absolute": now + self._absolute,
                    "instance": self.instance_id,
                    "now": now,
                },
            )
            row = cur.fetchone()
        assert row is not None
        session = Session(
            id=session_id,
            principal_id=identity.principal_id,
            account_id=identity.account_id,
            issuer=identity.issuer,
            subject=identity.subject,
            csrf_token_hash=_digest(csrf_token),
            created_at=row["created_at"],
            last_seen_at=row["created_at"],
            idle_expires_at=row["idle_expires_at"],
            absolute_expires_at=row["absolute_expires_at"],
        )
        return IssuedSession(
            session=session,
            cookie_value=f"{session_id}.{cookie_secret}",
            csrf_token=csrf_token,
        )

    def load(self, cookie_value: str | None, *, now: dt.datetime) -> Session:
        """Resolve a cookie to a live session, or raise :class:`SessionMissing`.

        One statement does the whole check: the digest comparison, the
        revocation check and both expiry checks, then the idle-window
        extension. There is no window between "is this valid" and "record that
        it was used" for a concurrent request to slip through.
        """
        session_id, secret = _split_cookie(cookie_value)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb.browser_session
                   SET last_seen_at = %(now)s,
                       last_served_by_instance = %(instance)s,
                       idle_expires_at = GREATEST(idle_expires_at, %(idle)s)
                 WHERE id = %(id)s
                   AND secret_hash = %(secret_hash)s
                   AND revoked_at IS NULL
                   AND %(now)s < idle_expires_at
                   AND %(now)s < absolute_expires_at
                RETURNING id, principal_id, account_id, issuer, subject,
                          csrf_token_hash, created_at, last_seen_at,
                          idle_expires_at, absolute_expires_at
                """,
                {
                    "id": session_id,
                    "secret_hash": _digest(secret),
                    "instance": self.instance_id,
                    "idle": now + self._idle,
                    "now": now,
                },
            )
            row = cur.fetchone()
        if row is None:
            raise SessionMissing("no_live_session")
        return Session(
            id=row["id"],
            principal_id=row["principal_id"],
            account_id=row["account_id"],
            issuer=row["issuer"],
            subject=row["subject"],
            csrf_token_hash=row["csrf_token_hash"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            idle_expires_at=row["idle_expires_at"],
            absolute_expires_at=row["absolute_expires_at"],
        )

    def revoke(self, session_id: UUID, *, reason: str, now: dt.datetime) -> bool:
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb.browser_session
                   SET revoked_at = %(now)s, revoked_reason = %(reason)s
                 WHERE id = %(id)s AND revoked_at IS NULL
                """,
                {"id": session_id, "reason": reason, "now": now},
            )
            return cur.rowcount == 1

    def purge(self, *, now: dt.datetime) -> int:
        """Drop rows that can no longer authenticate anything.

        Revoked rows are kept for a week so that a support question ("was this
        session live at 14:03?") has an answer. Token retention is not the
        same as credential retention, and neither is unbounded.
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM kb.browser_session
                 WHERE absolute_expires_at < %(now)s
                    OR (revoked_at IS NOT NULL AND revoked_at < %(now)s - interval '7 days')
                """,
                {"now": now},
            )
            return cur.rowcount


def _split_cookie(cookie_value: str | None) -> tuple[UUID, str]:
    if not cookie_value or "." not in cookie_value:
        raise SessionMissing("no_cookie")
    raw_id, _, secret = cookie_value.partition(".")
    if not secret:
        raise SessionMissing("no_cookie")
    try:
        return UUID(raw_id), secret
    except ValueError as exc:
        # A malformed id is not an identity. Same answer as a wrong secret.
        raise SessionMissing("malformed_cookie") from exc


def csrf_matches(session: Session, header_value: str | None, cookie_value: str | None) -> bool:
    """Double-submit CSRF, bound to the session.

    Both the header and the cookie must be present, must be equal, and the
    shared value must hash to the digest stored on *this* session. Two
    conditions matter: an attacker who can plant a cookie in the victim's
    browser still cannot produce a value that matches a digest they did not
    choose, and a token from one session cannot drive another.
    """
    if not header_value or not cookie_value:
        return False
    if not hmac.compare_digest(header_value, cookie_value):
        return False
    return hmac.compare_digest(_digest(header_value), session.csrf_token_hash)
