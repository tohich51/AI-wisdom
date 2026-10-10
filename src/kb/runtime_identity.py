"""Resolve request identity from a verified bearer token or browser session."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

from fastapi import HTTPException, Request

from kb.access.identity import IdentityRejected, VerifiedIdentity
from kb.access.policy import Principal, transaction_identity
from kb.access.session import SessionIdentityIncomplete, SessionMissing
from kb.http.auth_routes import require_principal, session_cookie_name

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
    pool: Any,
    store: Any,
    settings: Any,
    bearer_verifier: Any,
) -> tuple[Any, Any]:
    """Create the request resolver and standalone bearer-token resolver."""

    def clock() -> datetime:
        return datetime.now(UTC)

    cookie_dependency = require_principal(store, settings, clock)

    def admit(identity: VerifiedIdentity, principal: Principal) -> None:
        try:
            with pool.connection() as connection:
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
                principal = Principal(identity.principal_id, identity.account_id)
            except (AttributeError, TypeError, ValueError):
                _raise_unauthorized()

            state.principal = principal
            state.identity = identity
            state._kb_identity_resolved = True
            return principal

        try:
            cookie_dependency(
                request,
                x_kb_csrf=request.headers.get("x-kb-csrf"),
            )
        except (SessionMissing, SessionIdentityIncomplete):
            _raise_unauthorized()
        except HTTPException:
            raise
        except Exception:
            _raise_service_unavailable()

        cookie_name = session_cookie_name(settings)
        cookie = request.cookies.get(cookie_name)
        if not isinstance(cookie, str) or not cookie:
            _raise_unauthorized()

        try:
            session = store.load(cookie, now=clock())
        except (SessionMissing, SessionIdentityIncomplete):
            _raise_unauthorized()
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
        except (SessionMissing, SessionIdentityIncomplete):
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
