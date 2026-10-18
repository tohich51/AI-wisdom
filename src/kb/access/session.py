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

Leases
------
Every statement here runs on a lease, never on a connection the store owns.
:meth:`SessionStore.use_shared_connections` points the store at the one
connection factory the application already has, so a session read is a lease
from the same pool as every other SQL phase instead of a fresh TCP connection
and a fresh authentication handshake. The role is taken with ``SET LOCAL
ROLE`` inside the lease's own transaction, so it reverts when the lease ends;
``SET ROLE`` would outlive it, and a pooled connection that answers the next
request as ``kb_app`` is a request that has lost the identity every policy
decides on. The store does not cache a session, a permission or a connection
beyond the block it is running: a resolve is a resolve, every time.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.access.identity import VerifiedIdentity
from kb.access.logout import VerifiedLogout
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
SUPPORTED_AUTHORITY_SCOPES: Final[frozenset[str]] = frozenset(
    {"kb:read", "kb:contribute", "kb:experience", "kb:export", "kb:admin"}
)
_BROWSER_PAIRING_TTL: Final = dt.timedelta(seconds=120)
# ASCII "KBP_PAIR" in PostgreSQL's process-global advisory-lock keyspace.
_BROWSER_PAIRING_ADVISORY_LOCK: Final[int] = 0x4B42505F50414952
_BROWSER_PAIRING_RATE_LIMIT: Final[int] = 64
_BROWSER_PAIRING_ROW_LIMIT: Final[int] = 128
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


class BrowserPairingBusy(Exception):
    """The bounded browser-pairing admission limit is currently reached."""


class SessionLeaseMisuse(Exception):
    """A lease was asked for inside a transaction somebody else opened.

    The gateway role is taken for the length of one lease. Taken inside
    another transaction it would apply to the rest of that transaction, so the
    store refuses rather than quietly downgrading whatever runs next.
    """


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


_IDENTITY_JSON_FIELDS: Final = frozenset(
    {
        "issuer",
        "subject",
        "principal_id",
        "account_id",
        "scopes",
        "client_id",
        "issued_at",
        "expires_at",
        "provider_session_id",
    }
)


def _aware_datetime(value: Any, name: str) -> dt.datetime:
    if not isinstance(value, dt.datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_must_be_timezone_aware")
    return value


def _credential_cap(now: dt.datetime, value: dt.datetime | None) -> dt.datetime:
    if value is None:
        raise ValueError("derived_session_requires_credential_expiry")
    cap = _aware_datetime(value, "credential_expires_at")
    if cap <= now:
        raise ValueError("credential_expiry_already_elapsed")
    return cap


def _validate_authority_scopes(
    value: frozenset[str], identity: VerifiedIdentity | None = None
) -> frozenset[str]:
    if not isinstance(value, frozenset):
        raise ValueError("authority_scopes_must_be_a_frozenset")
    if not value or not value <= SUPPORTED_AUTHORITY_SCOPES or "kb:read" not in value:
        raise ValueError("invalid_authority_scope_ceiling")
    if identity is not None:
        identity_scopes = identity.scopes
        if not isinstance(identity_scopes, frozenset) or any(
            not isinstance(scope, str) for scope in identity_scopes
        ):
            raise ValueError("invalid_identity_scopes")
        if not value <= identity_scopes:
            raise ValueError("authority_scope_exceeds_verified_identity")
    return value


def _serialise_identity(identity: VerifiedIdentity, *, now: dt.datetime) -> dict[str, Any]:
    if not isinstance(identity, VerifiedIdentity):
        raise ValueError("pairing_identity_must_be_verified")
    if not isinstance(identity.principal_id, UUID):
        raise ValueError("pairing_principal_invalid")
    if not isinstance(identity.account_id, UUID):
        raise ValueError("pairing_account_required")
    for field in ("issuer", "subject"):
        value = getattr(identity, field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"pairing_{field}_invalid")
    if not isinstance(identity.scopes, frozenset) or any(
        not isinstance(scope, str) for scope in identity.scopes
    ):
        raise ValueError("pairing_scopes_invalid")
    for field in ("client_id", "provider_session_id"):
        value = getattr(identity, field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"pairing_{field}_invalid")
    issued_at = _aware_datetime(identity.issued_at, "pairing_issued_at")
    expires_at = _aware_datetime(identity.expires_at, "pairing_expires_at")
    if expires_at <= issued_at or expires_at <= now:
        raise ValueError("pairing_identity_expired_or_inverted")
    return {
        "issuer": identity.issuer,
        "subject": identity.subject,
        "principal_id": str(identity.principal_id),
        "account_id": str(identity.account_id),
        "scopes": sorted(identity.scopes),
        "client_id": identity.client_id,
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "provider_session_id": identity.provider_session_id,
    }


def _identity_from_json(payload: Any) -> VerifiedIdentity:
    try:
        if not isinstance(payload, dict) or set(payload) != _IDENTITY_JSON_FIELDS:
            raise ValueError
        for field in ("issuer", "subject"):
            if not isinstance(payload[field], str) or not payload[field].strip():
                raise ValueError
        principal_id = UUID(payload["principal_id"])
        account_id = UUID(payload["account_id"])
        raw_scopes = payload["scopes"]
        if (
            not isinstance(raw_scopes, list)
            or any(not isinstance(scope, str) for scope in raw_scopes)
            or len(raw_scopes) != len(set(raw_scopes))
        ):
            raise ValueError
        for field in ("client_id", "provider_session_id"):
            value = payload[field]
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError
        issued_at = dt.datetime.fromisoformat(payload["issued_at"])
        expires_at = dt.datetime.fromisoformat(payload["expires_at"])
        if (
            issued_at.tzinfo is None
            or issued_at.utcoffset() is None
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or expires_at <= issued_at
        ):
            raise ValueError
        return VerifiedIdentity(
            issuer=payload["issuer"],
            subject=payload["subject"],
            principal_id=principal_id,
            account_id=account_id,
            scopes=frozenset(raw_scopes),
            issued_at=issued_at,
            expires_at=expires_at,
            provider_session_id=payload["provider_session_id"],
            client_id=payload["client_id"],
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise SessionIdentityIncomplete("browser_pairing_identity_invalid") from exc


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
class BrowserPairingRequest:
    id: UUID
    nonce: str
    binding: str
    expires_at: dt.datetime


@dataclass(frozen=True)
class BrowserPairingApproval:
    identity: VerifiedIdentity
    authority_scopes: frozenset[str]
    authority_cloud_client: bool
    credential_expires_at: dt.datetime
    return_to: str | None


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
    authority_scopes: frozenset[str] | None = None
    authority_cloud_client: bool = False

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
            cloud_client=self.authority_cloud_client,
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
        # Set once, at wiring time, by the single auth owner. Until then the
        # store keeps opening its own connection, which is the behaviour it
        # always had.
        self._connections: Callable[[], Any] | None = None

    # -- plumbing -------------------------------------------------------

    def use_shared_connections(self, factory: Callable[[], Any]) -> None:
        """Take this store's leases from the application's connection factory.

        ``factory`` is the callable context manager that returns
        ``pool.connection()``: the one F established and the gateway
        publishes. Calling this once, at wiring time, turns every session read
        into one lease from the pool the rest of the application already uses,
        instead of a connection opened, authenticated and closed per request.

        It is deliberately not a constructor argument and deliberately not
        called per request. The store is built in the gateway's wiring, and
        the single auth owner is the one place that knows both objects. A
        store that was never told keeps its own connection, which is what the
        gateway-independent callers have always had.
        """

        if not callable(factory):
            raise TypeError("connection factory must be callable")
        self._connections = factory

    @contextlib.contextmanager
    def _dedicated_connection(self) -> Iterator[Connection[dict[str, Any]]]:
        """The unbound path: one private connection, opened and closed here."""
        conn: Connection[dict[str, Any]] = Connection.connect(
            self._dsn, autocommit=True, row_factory=dict_row
        )
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _as_gateway_role(self, conn: Any) -> Iterator[Any]:
        """Run the block as kb_app, for the length of this lease only.

        ``SET LOCAL`` inside a transaction, not ``SET ROLE``. The role
        reverts at the commit, so a pooled connection goes back to the pool
        answering as the role it connected with; a session-scoped ``SET
        ROLE`` would leave the next request running with the privileges of
        the previous one, which is the same hazard C06's transaction-local
        identity GUCs exist to prevent.
        """

        if not self._role:
            yield conn
            return
        if conn.info.transaction_status is not TransactionStatus.IDLE:
            raise SessionLeaseMisuse("session_lease_inside_an_open_transaction")
        with conn.transaction():
            with conn.cursor() as cur:
                # The gateway reads credential material as kb_app, the
                # unprivileged role. Running the store as the table owner
                # would prove nothing about the privilege model in 0003. The
                # interpolated name is one of two literals chosen by the
                # caller, never request input.
                cur.execute(f"SET LOCAL ROLE {self._role}")
            yield conn

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Connection[dict[str, Any]]]:
        """One lease, with this store's row shape and role, and nothing kept.

        The lease belongs to whoever leased it: the pooled connection is
        handed straight back when the block ends, and the store holds no
        reference to it afterwards. Nothing here survives a return either --
        no session, no principal, no permission.
        """

        if self._connections is None:
            with self._dedicated_connection() as conn:
                with self._as_gateway_role(conn):
                    yield conn
            return

        with self._connections() as pooled:
            # The shared pool hands out connections with the default row
            # factory; these statements read by column name. That is a client
            # side property, restored on the way out so the next borrower of
            # this connection gets the pool's own shape back.
            previous = pooled.row_factory
            pooled.row_factory = dict_row
            try:
                with self._as_gateway_role(pooled):
                    yield pooled
            finally:
                pooled.row_factory = previous

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

    def _lock_provider_session(self, cur: Any, issuer: str, sid: str) -> None:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{len(issuer)}:{issuer}{sid}",),
        )

    def _guard_provider_session(self, cur: Any, identity: VerifiedIdentity) -> None:
        sid = identity.provider_session_id
        if not sid:
            return

        self._lock_provider_session(cur, identity.issuer, sid)
        cur.execute(
            """
            SELECT 1
            FROM kb.oidc_logged_out_session
            WHERE issuer = %s
              AND provider_session_id = %s
              AND (subject IS NULL OR subject = %s)
            """,
            (identity.issuer, sid, identity.subject),
        )
        if cur.fetchone() is not None:
            raise SessionMissing("provider_session_logged_out")

    def apply_provider_logout(self, logout: VerifiedLogout, *, now: dt.datetime) -> int:
        with self._connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._lock_provider_session(cur, logout.issuer, logout.session_id)

            cur.execute(
                """
                INSERT INTO kb.oidc_logout_event (
                    issuer,
                    client_id,
                    jti,
                    provider_session_id,
                    subject,
                    issued_at,
                    expires_at,
                    received_at
                )
                VALUES (
                    %(issuer)s,
                    %(audience)s,
                    %(jti)s,
                    %(session_id)s,
                    %(subject)s,
                    %(issued_at)s,
                    %(expires_at)s,
                    %(now)s
                )
                ON CONFLICT DO NOTHING
                RETURNING jti
                """,
                {
                    "issuer": logout.issuer,
                    "audience": logout.audience,
                    "jti": logout.jti,
                    "session_id": logout.session_id,
                    "subject": logout.subject,
                    "issued_at": logout.issued_at,
                    "expires_at": logout.expires_at,
                    "now": now,
                },
            )
            if cur.fetchone() is None:
                return 0

            cur.execute(
                """
                INSERT INTO kb.oidc_logged_out_session (
                    issuer,
                    provider_session_id,
                    subject,
                    recorded_at
                )
                VALUES (
                    %(issuer)s,
                    %(session_id)s,
                    %(subject)s,
                    %(now)s
                )
                ON CONFLICT (issuer, provider_session_id) DO UPDATE
                SET subject = CASE
                    WHEN EXCLUDED.subject IS NULL THEN NULL
                    ELSE kb.oidc_logged_out_session.subject
                END
                """,
                {
                    "issuer": logout.issuer,
                    "session_id": logout.session_id,
                    "subject": logout.subject,
                    "now": now,
                },
            )

            cur.execute(
                """
                UPDATE kb.browser_session
                SET revoked_at = %(now)s,
                    revoked_reason = 'provider_logout'
                WHERE issuer = %(issuer)s
                  AND provider_session_id = %(sid)s
                  AND revoked_at IS NULL
                  AND (%(subject)s::text IS NULL OR subject = %(subject)s)
                """,
                {
                    "now": now,
                    "issuer": logout.issuer,
                    "sid": logout.session_id,
                    "subject": logout.subject,
                },
            )
            return cur.rowcount

    def create_session(
        self,
        identity: VerifiedIdentity,
        *,
        now: dt.datetime,
        authority_scopes: frozenset[str] | None = None,
        authority_cloud_client: bool = False,
        credential_expires_at: dt.datetime | None = None,
    ) -> IssuedSession:
        if type(authority_cloud_client) is not bool:
            raise ValueError("authority_cloud_client_must_be_boolean")
        scopes: frozenset[str] | None = None
        credential_cap: dt.datetime | None = None
        if authority_scopes is not None:
            now = _aware_datetime(now, "now")
            scopes = _validate_authority_scopes(authority_scopes, identity)
            credential_cap = min(_credential_cap(now, credential_expires_at), identity.expires_at)

        session_id = uuid4()
        cookie_secret = secrets.token_urlsafe(_SECRET_BYTES)
        csrf_token = secrets.token_urlsafe(_CSRF_BYTES)
        idle_expires_at = now + self._idle
        absolute_expires_at = now + self._absolute
        if credential_cap is not None:
            idle_expires_at = min(idle_expires_at, credential_cap)
            absolute_expires_at = min(absolute_expires_at, credential_cap)

        with self._connection() as conn, conn.transaction(), conn.cursor() as cur:
            self._guard_provider_session(cur, identity)
            cur.execute(
                """
                INSERT INTO kb.browser_session (
                    id, secret_hash, csrf_token_hash, principal_id, account_id,
                    issuer, subject, provider_session_id,
                    idle_expires_at, absolute_expires_at, created_by_instance,
                    created_at, last_seen_at, authority_scopes,
                    authority_cloud_client
                ) VALUES (
                    %(id)s, %(secret_hash)s, %(csrf_hash)s, %(principal_id)s, %(account_id)s,
                    %(issuer)s, %(subject)s, %(provider_session_id)s,
                    %(idle)s, %(absolute)s, %(instance)s, %(now)s, %(now)s,
                    %(authority_scopes)s, %(authority_cloud_client)s
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
                    "idle": idle_expires_at,
                    "absolute": absolute_expires_at,
                    "instance": self.instance_id,
                    "now": now,
                    "authority_scopes": sorted(scopes) if scopes is not None else None,
                    "authority_cloud_client": authority_cloud_client,
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
            authority_scopes=scopes,
            authority_cloud_client=authority_cloud_client,
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
                       idle_expires_at = LEAST(
                           absolute_expires_at, GREATEST(idle_expires_at, %(idle)s)
                       )
                 WHERE id = %(id)s
                   AND secret_hash = %(secret_hash)s
                   AND revoked_at IS NULL
                   AND %(now)s < idle_expires_at
                   AND %(now)s < absolute_expires_at
                RETURNING id, principal_id, account_id, issuer, subject,
                          csrf_token_hash, created_at, last_seen_at,
                          idle_expires_at, absolute_expires_at,
                          authority_scopes, authority_cloud_client
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
            authority_scopes=(
                frozenset(row["authority_scopes"]) if row["authority_scopes"] is not None else None
            ),
            authority_cloud_client=row["authority_cloud_client"],
        )

    def begin_browser_pairing(
        self, *, return_to: str | None, now: dt.datetime
    ) -> BrowserPairingRequest:
        now = _aware_datetime(now, "now")
        if return_to is not None and not isinstance(return_to, str):
            raise ValueError("return_to_must_be_string_or_null")
        with self._connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_BROWSER_PAIRING_ADVISORY_LOCK,),
            )
            cur.execute(
                """
                DELETE FROM kb.browser_pairing
                 WHERE expires_at <= %(now)s
                """,
                {"now": now},
            )
            cur.execute(
                """
                SELECT
                    count(*) FILTER (
                        WHERE created_at >= %(now)s - interval '60 seconds'
                          AND created_at <= %(now)s
                    ) AS recent_count,
                    count(*) FILTER (WHERE expires_at > %(now)s) AS live_count
                  FROM kb.browser_pairing
                """,
                {"now": now},
            )
            counts = cur.fetchone()
            recent_count, live_count = counts["recent_count"], counts["live_count"]
            if (
                recent_count >= _BROWSER_PAIRING_RATE_LIMIT
                or live_count >= _BROWSER_PAIRING_ROW_LIMIT
            ):
                raise BrowserPairingBusy

            pairing_id = uuid4()
            nonce = new_nonce()
            binding = new_browser_binding()
            expires_at = now + _BROWSER_PAIRING_TTL
            cur.execute(
                """
                INSERT INTO kb.browser_pairing (
                    id, binding_hash, nonce_hash, return_to, created_at, expires_at
                ) VALUES (
                    %(id)s, %(binding_hash)s, %(nonce_hash)s, %(return_to)s,
                    %(created_at)s, %(expires_at)s
                )
                """,
                {
                    "id": pairing_id,
                    "binding_hash": _digest(binding),
                    "nonce_hash": _digest(nonce),
                    "return_to": return_to,
                    "created_at": now,
                    "expires_at": expires_at,
                },
            )
        return BrowserPairingRequest(
            id=pairing_id,
            nonce=nonce,
            binding=binding,
            expires_at=expires_at,
        )

    def approve_browser_pairing(
        self,
        pairing_id: UUID,
        nonce: str,
        identity: VerifiedIdentity,
        *,
        authority_scopes: frozenset[str],
        authority_cloud_client: bool,
        credential_expires_at: dt.datetime,
        now: dt.datetime,
    ) -> bool:
        now = _aware_datetime(now, "now")
        identity_payload = _serialise_identity(identity, now=now)
        scopes = _validate_authority_scopes(authority_scopes, identity)
        credential_cap = min(_credential_cap(now, credential_expires_at), identity.expires_at)
        if type(authority_cloud_client) is not bool:
            raise ValueError("authority_cloud_client_must_be_boolean")

        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb.browser_pairing
                   SET approved_at = %(now)s,
                       approved_identity = %(identity)s,
                       authority_scopes = %(scopes)s,
                       authority_cloud_client = %(cloud_client)s,
                       credential_expires_at = LEAST(
                           %(credential_expires_at)s,
                           %(identity_expires_at)s
                       )
                 WHERE id = %(id)s
                   AND nonce_hash = %(nonce_hash)s
                   AND approved_at IS NULL
                   AND consumed_at IS NULL
                   AND cancelled_at IS NULL
                   AND %(now)s < expires_at
                """,
                {
                    "id": pairing_id,
                    "nonce_hash": _digest(nonce),
                    "identity": Jsonb(identity_payload),
                    "scopes": sorted(scopes),
                    "cloud_client": authority_cloud_client,
                    "credential_expires_at": credential_cap,
                    "identity_expires_at": identity.expires_at,
                    "now": now,
                },
            )
            return cur.rowcount == 1

    def consume_browser_pairing(
        self, pairing_id: UUID, nonce: str, *, binding: str, now: dt.datetime
    ) -> BrowserPairingApproval | None:
        now = _aware_datetime(now, "now")
        try:
            with self._connection() as conn, conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE kb.browser_pairing
                       SET consumed_at = %(now)s
                     WHERE id = %(id)s
                       AND binding_hash = %(binding_hash)s
                       AND nonce_hash = %(nonce_hash)s
                       AND approved_at IS NOT NULL
                       AND consumed_at IS NULL
                       AND cancelled_at IS NULL
                       AND %(now)s < expires_at
                       AND %(now)s < credential_expires_at
                    RETURNING approved_identity, authority_scopes,
                              authority_cloud_client, credential_expires_at,
                              return_to
                    """,
                    {
                        "id": pairing_id,
                        "binding_hash": _digest(binding),
                        "nonce_hash": _digest(nonce),
                        "now": now,
                    },
                )
                row = cur.fetchone()
                if row is None:
                    return None

                identity = _identity_from_json(row["approved_identity"])
                try:
                    scopes = _validate_authority_scopes(frozenset(row["authority_scopes"]))
                    credential_expires_at = _credential_cap(now, row["credential_expires_at"])
                    if not scopes <= identity.scopes:
                        raise ValueError
                    if type(row["authority_cloud_client"]) is not bool:
                        raise ValueError
                    if identity.expires_at <= now or credential_expires_at > identity.expires_at:
                        raise ValueError
                    return_to = row["return_to"]
                    if return_to is not None and not isinstance(return_to, str):
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise SessionIdentityIncomplete("browser_pairing_approval_invalid") from exc

                self._guard_provider_session(cur, identity)
        except SessionIdentityIncomplete:
            return None

        return BrowserPairingApproval(
            identity=identity,
            authority_scopes=scopes,
            authority_cloud_client=row["authority_cloud_client"],
            credential_expires_at=credential_expires_at,
            return_to=return_to,
        )

    def cancel_browser_pairing(self, pairing_id: UUID, *, binding: str, now: dt.datetime) -> bool:
        now = _aware_datetime(now, "now")
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kb.browser_pairing
                   SET cancelled_at = %(now)s
                 WHERE id = %(id)s
                   AND binding_hash = %(binding_hash)s
                   AND consumed_at IS NULL
                   AND cancelled_at IS NULL
                """,
                {
                    "id": pairing_id,
                    "binding_hash": _digest(binding),
                    "now": now,
                },
            )
            return cur.rowcount == 1

    def cancel_browser_pairings(self, *, binding: str, now: dt.datetime) -> None:
        """Logout cancels only pending authorizations bound to this browser."""
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE kb.browser_pairing SET cancelled_at=%s "
                "WHERE binding_hash=%s AND consumed_at IS NULL AND cancelled_at IS NULL",
                (now, _digest(binding)),
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

    def purge_browser_pairings(self, *, now: dt.datetime) -> int:
        """Drop expired pairings while retaining every unexpired terminal row."""
        now = _aware_datetime(now, "now")
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM kb.browser_pairing
                 WHERE expires_at <= %(now)s
                """,
                {"now": now},
            )
            return cur.rowcount

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
            session_count = cur.rowcount
        pairing_count = self.purge_browser_pairings(now=now)
        return session_count + pairing_count


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
