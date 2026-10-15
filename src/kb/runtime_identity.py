"""The shared connection lease, and request identity from a verified token.

Two things live here because they are the same guarantee seen from two ends.
A pooled connection is only useful with the caller's identity already set on
it, and that identity is transaction-local, so it has to be re-established for
every SQL phase rather than inherited from whatever ran before on that
connection. The factory is therefore defined next to the identity helper, and
the pair is the contract the HTTP router, the MCP transport and the readiness
probe all lease from.

The contract, in the frozen words: a callable context manager returning
``pool.connection()``; the lease is owned by the orchestration that called it;
an operation handed a ``conn`` never closes it; identity is established
through the existing ``transaction_identity`` for each SQL phase.

The same reasoning decides how often a request resolves a session: once. The
session is loaded, the CSRF answer is taken from that load, and the identity is
built from that session -- one row, one read, one answer per request. What is
remembered afterwards lives on ``request.state`` and dies with the request.
There is no cache of sessions or of permissions that outlives it, because a
cached permission is a permission that survives the logout meant to take it
away.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

from fastapi import HTTPException, Request

from kb.access.bearer_policy import bearer_principal, require_scope, scope_for_request
from kb.access.identity import IdentityRejected, VerifiedIdentity
from kb.access.policy import Principal, transaction_identity
from kb.access.session import CSRF_HEADER, SessionIdentityIncomplete
from kb.http.auth_routes import require_session

#: A zero-argument callable returning a context manager over one connection.
#: Calling it checks a connection out; entering the result is what acquires it.
ConnectionFactory = Callable[[], Any]


def build_connection_factory(pool: Any) -> ConnectionFactory:
    """The single construction path for a pooled connection.

    Everything else in the application takes the returned callable, not the
    pool. The pool is built, opened and closed in one place -- the gateway --
    and is never stored on ``app.state``, so there is nothing for a route or a
    tool to reach past the factory with. That is the difference between a
    factory that exists and a factory that is the only way in: a second
    ``pool.connection()`` anywhere else would be a connection with no
    established identity and no shared lease, which is exactly what this
    contract exists to prevent.

    The returned object is a plain context manager, not a connection, so a
    caller cannot forget to release the lease by forgetting a method call.
    """

    def connection() -> Any:
        return pool.connection()

    return connection


@contextmanager
def identity_connection(factory: ConnectionFactory, principal: Principal) -> Iterator[Any]:
    """One SQL phase: a connection with ``principal``'s identity established.

    A short plan, an external call, then a second plan that must see current
    ACLs and generation state is not one long transaction. Each phase opens its
    own lease, so the external I/O happens with no connection held, and each
    phase re-establishes identity rather than trusting the previous one. The
    operation that receives the connection still does not close it: the lease
    belongs to this block.
    """
    with factory() as conn:
        with transaction_identity(conn, principal):
            yield conn


_MEMBERSHIP_SQL = (
    "SELECT 1 FROM kb.membership "
    "WHERE organisation_id=%s AND principal_id=%s "
    "AND issuer=%s AND subject=%s AND status='active'"
)
_INVITATION_ACCEPT_PATH = re.compile(
    r"/api/orgs/(?P<account>[^/]+)/invitations/(?P<invitation>[^/]+)/accept"
)


def _raise_unauthorized() -> NoReturn:
    raise HTTPException(status_code=401, detail="Unauthorized") from None


def _raise_forbidden() -> NoReturn:
    raise HTTPException(status_code=403, detail="Forbidden") from None


def _raise_service_unavailable() -> NoReturn:
    raise HTTPException(status_code=503, detail="Service Unavailable") from None


def _header_values(headers: Any, name: str) -> list[Any]:
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = list(getlist(name))
        if values:
            return values

    items = getattr(headers, "items", None)
    if callable(items):
        return [value for key, value in items() if isinstance(key, str) and key.lower() == name]

    try:
        value = headers.get(name)
    except AttributeError:
        return []
    return [] if value is None else [value]


def _canonical_uuid(value: str) -> UUID | None:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    if str(parsed) != value.lower():
        return None
    return parsed


def _is_pending_browser_invitation_accept(request: Any, account_id: UUID) -> bool:
    if request.method != "POST":
        return False

    match = _INVITATION_ACCEPT_PATH.fullmatch(request.url.path)
    if match is None:
        return False

    path_account = _canonical_uuid(match.group("account"))
    invitation_id = _canonical_uuid(match.group("invitation"))
    if path_account is None or invitation_id is None:
        return False

    try:
        expected_account = UUID(str(account_id))
    except (AttributeError, TypeError, ValueError):
        return False
    return path_account == expected_account


def create_resolver(
    connections: ConnectionFactory,
    store: Any,
    settings: Any,
    bearer_verifier: Any,
) -> tuple[Any, Any]:
    """Create the request resolver and standalone bearer-token resolver.

    This is the single auth owner. It is the one object that holds both the
    connection factory and the session store, so it is also where the store is
    told to lease from that factory -- once, here, instead of in every request.
    The binding is duck-typed on purpose: a resolver built with something that
    is not a session store, which the bearer path never needs, must still
    construct.
    """

    def clock() -> datetime:
        return datetime.now(UTC)

    cookie_dependency = require_session(store, settings, clock)

    lease_from = getattr(store, "use_shared_connections", None)
    if callable(lease_from):
        lease_from(connections)

    def admit(identity: VerifiedIdentity, principal: Principal) -> None:
        try:
            with connections() as connection:
                with transaction_identity(connection, principal):
                    with connection.cursor() as cursor:
                        cursor.execute(
                            _MEMBERSHIP_SQL,
                            (
                                str(identity.account_id),
                                str(identity.principal_id),
                                identity.issuer,
                                identity.subject,
                            ),
                        )
                        is_member = cursor.fetchone() is not None
        except Exception:
            _raise_service_unavailable()

        if not is_member:
            _raise_forbidden()

    def resolve_token(token: str) -> VerifiedIdentity:
        if not isinstance(token, str) or not token.strip():
            _raise_unauthorized()

        try:
            identity = bearer_verifier.verify(token)
        except IdentityRejected:
            _raise_unauthorized()
        except HTTPException:
            raise
        except Exception:
            _raise_service_unavailable()

        if identity.account_id is None:
            _raise_unauthorized()

        try:
            principal = Principal(identity.principal_id, identity.account_id)
        except (AttributeError, TypeError, ValueError):
            _raise_unauthorized()

        admit(identity, principal)
        return identity

    def resolve(request: Request) -> Principal:
        state = request.state
        if getattr(state, "_kb_identity_resolved", False):
            return state.principal

        authorization_values = _header_values(request.headers, "authorization")
        if authorization_values:
            if len(authorization_values) != 1:
                _raise_unauthorized()
            authorization = authorization_values[0]
            if not isinstance(authorization, str):
                _raise_unauthorized()

            parts = authorization.split()
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
                _raise_unauthorized()

            identity = resolve_token(parts[1])
            if identity.account_id is None:
                _raise_unauthorized()
            try:
                require_scope(identity, scope_for_request(request.method, request.url.path))
                principal = bearer_principal(identity)
            except PermissionError:
                _raise_forbidden()
            except (AttributeError, TypeError, ValueError):
                _raise_unauthorized()

            state.principal = principal
            state.identity = identity
            state._kb_identity_resolved = True
            return principal

        # One resolve. The CSRF answer and the identity below both come out of
        # the same load, so the row is written once instead of twice, the
        # connection is leased once instead of twice, and no second snapshot
        # of the session is admitted between the two answers.
        try:
            session = cookie_dependency(
                request,
                x_kb_csrf=request.headers.get(CSRF_HEADER),
            )
        except HTTPException:
            raise
        except Exception:
            _raise_service_unavailable()

        try:
            principal = session.to_principal()
            identity = VerifiedIdentity(
                issuer=session.issuer,
                subject=session.subject,
                principal_id=session.principal_id,
                account_id=session.account_id,
                scopes=frozenset(),
                issued_at=session.created_at,
                expires_at=session.absolute_expires_at,
            )
        except SessionIdentityIncomplete:
            _raise_unauthorized()
        except (AttributeError, TypeError, ValueError):
            _raise_unauthorized()

        if (
            identity.principal_id is None
            or identity.account_id is None
            or not identity.issuer
            or not identity.subject
            or identity.principal_id != principal.principal_id
            or identity.account_id != principal.account_id
        ):
            _raise_unauthorized()

        if not _is_pending_browser_invitation_accept(request, principal.account_id):
            admit(identity, principal)

        state.principal = principal
        state.identity = identity
        state._kb_identity_resolved = True
        return principal

    return resolve, resolve_token
