"""C07 — the browser-facing HTTP surface for identity.

Four endpoints, and none of them accepts a caller-asserted identity:

===========================  ============================================
``GET  /auth/login``         302 to the provider, PKCE + state + nonce
``GET  /auth/callback``      redeem the code, set the session cookies
``POST /auth/logout``        revoke the session (CSRF protected)
``GET  /auth/session``       "am I signed in", and as whom
===========================  ============================================

The identity of a request is the session cookie and nothing else. There is no
``X-User-Id``, no ``user_id`` form field and no header this module reads to
decide who is calling. :func:`require_principal` is the only way a handler
obtains a :class:`kb.access.policy.Principal`, and the only way to get one is
to present a cookie that a callback minted.

Cookie shape
------------
``__Host-kb_session``  HttpOnly, Secure, SameSite=Lax, Path=/. ``SameSite=Lax``
rather than ``Strict`` because the callback arrives as a top-level navigation
from the provider's origin, and ``Strict`` would drop the cookie on exactly
that request. The ``__Host-`` prefix requires ``Secure``, so the unprefixed
name is used when ``KB_SESSION_COOKIE_SECURE=false``; that configuration is a
local-development affordance and is named as one here.

``kb_csrf``            NOT HttpOnly, by design: the browser has to read it to
                       echo it back. It is safe as a readable cookie only
                       because the expected value is also stored, hashed, on
                       the session row — see :func:`kb.access.session.csrf_matches`.

Responses carry no token. ``/auth/session`` returns identifiers and an
expiry; the authorization code, the ID token, the refresh token and the
cookie value itself are never placed in a response body.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from collections.abc import Callable, Iterator
from typing import Any, Final, Protocol

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from kb.access.policy import Principal, transaction_identity
from kb.access.session import (
    CSRF_COOKIE,
    CSRF_HEADER,
    CSRF_PROTECTED_METHODS,
    SESSION_COOKIE,
    IssuedSession,
    Session,
    SessionMissing,
    csrf_matches,
)
from kb.http.auth_config import AuthSettings
from kb.http.auth_flow import AuthorizationCodeFlow, LoginRejected

_log = logging.getLogger("kb.auth")

#: Short-lived cookie that binds a callback to the browser which started the
#: login. This is the login-CSRF defence; see the migration for why `state`
#: alone is not enough.
LOGIN_BINDING_COOKIE: Final[str] = "kb_login"

#: Where the browser lands when no return target was supplied. A path, never a
#: host: a missing return target must not become an open redirect.
DEFAULT_RETURN_TARGET: Final[str] = "/"

#: Error bodies. Fixed strings, no provider text, no token, no exception repr.
_BODY_UNAUTHENTICATED: Final[dict[str, Any]] = {"error": "not_authenticated"}
_BODY_CSRF: Final[dict[str, Any]] = {"error": "csrf_token_required"}


class SessionSource(Protocol):
    """What the routes need from the session store.

    :class:`kb.access.session.SessionStore` is the only implementation. Naming
    it as a protocol keeps the two things that matter in the type: the routes
    read a session out of the database, and they never hold one in memory.
    """

    def load(self, cookie_value: str | None, *, now: dt.datetime) -> Session: ...


def login_cookie_name(settings: AuthSettings) -> str:
    return f"__Host-{LOGIN_BINDING_COOKIE}" if settings.cookie_secure else LOGIN_BINDING_COOKIE


def session_cookie_name(settings: AuthSettings) -> str:
    return f"__Host-{SESSION_COOKIE}" if settings.cookie_secure else SESSION_COOKIE


def set_session_cookies(response: Response, issued: IssuedSession, settings: AuthSettings) -> None:
    """Attach the session and CSRF cookies.

    ``max_age`` is the *absolute* lifetime, not the idle window. A browser
    that keeps sending the cookie past the idle deadline still gets nothing:
    :meth:`kb.access.session.SessionStore.load` checks both deadlines on
    every request. The cookie's own expiry is a convenience for the browser,
    not the authority.
    """
    max_age = int(settings.session_absolute_hours * 3600)
    response.set_cookie(
        session_cookie_name(settings),
        issued.cookie_value,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        issued.csrf_token,
        max_age=max_age,
        # Not HttpOnly: the browser has to read this one to echo it.
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, settings: AuthSettings) -> None:
    response.delete_cookie(session_cookie_name(settings), path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _error(code: str, status: int) -> JSONResponse:
    # The body is the fixed ``code`` only. A login failure must not tell the
    # caller whether the account exists, and must not carry a provider's
    # error string back out of the system.
    return JSONResponse({"error": code}, status_code=status)


def create_auth_router(
    *,
    flow: AuthorizationCodeFlow,
    store: SessionSource,
    settings: AuthSettings,
    clock: Callable[[], dt.datetime],
) -> APIRouter:
    """Build the router. One router per gateway process."""

    def load_session(request: Request) -> Session:
        return store.load(request.cookies.get(session_cookie_name(settings)), now=clock())

    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.get("/login")
    def login(
        return_to: str | None = Query(default=None),
    ) -> RedirectResponse:
        started = flow.start(return_to=return_to, now=clock())
        redirect = RedirectResponse(started.authorization_url, status_code=302)
        redirect.set_cookie(
            login_cookie_name(settings),
            started.browser_binding,
            max_age=int(settings.login_ttl_minutes * 60),
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return redirect

    @router.get("/callback")
    def callback(
        request: Request,
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
    ) -> Response:
        binding = request.cookies.get(login_cookie_name(settings))
        try:
            completed = flow.complete(code=code, state=state, binding=binding, now=clock())
        except LoginRejected as exc:
            return _error(exc.code, 400)
        redirect = RedirectResponse(completed.return_to or DEFAULT_RETURN_TARGET, status_code=302)
        # The binding has been consumed. Remove it whether or not the browser
        # sent it back.
        redirect.delete_cookie(login_cookie_name(settings), path="/")
        set_session_cookies(redirect, completed.issued, settings)
        return redirect

    @router.post("/logout")
    def logout(
        request: Request,
        x_kb_csrf: str | None = Header(default=None, alias=CSRF_HEADER),
    ) -> Response:
        try:
            session = load_session(request)
        except SessionMissing:
            # Signing out when already signed out is a success, not an error,
            # and it must not need a CSRF token that no session exists to
            # issue. There is nothing to protect: no cookie, no rows touched.
            response = JSONResponse({"status": "signed_out"}, status_code=200)
            clear_session_cookies(response, settings)
            return response
        if request.method.upper() in CSRF_PROTECTED_METHODS and not csrf_matches(
            session, x_kb_csrf, request.cookies.get(CSRF_COOKIE)
        ):
            _log.warning(
                "auth.csrf.rejected",
                extra={"event": "csrf_rejected", "session_id": str(session.id)},
            )
            return JSONResponse(_BODY_CSRF, status_code=403)
        flow.logout(session, now=clock())
        response = JSONResponse({"status": "signed_out"}, status_code=200)
        clear_session_cookies(response, settings)
        return response

    @router.get("/session")
    def whoami(request: Request) -> Response:
        """What the browser is allowed to know about itself.

        Identifiers and deadlines only. No token, no cookie value, no claims
        the caller did not already hold.
        """
        try:
            session = load_session(request)
        except SessionMissing:
            return JSONResponse(
                {"authenticated": False, "principal_id": None, "account_id": None},
                status_code=200,
            )
        return JSONResponse(
            {
                "authenticated": True,
                "principal_id": str(session.principal_id),
                "account_id": str(session.account_id) if session.account_id else None,
                "issuer": session.issuer,
                "expires_at": session.absolute_expires_at.isoformat(),
                "idle_expires_at": session.idle_expires_at.isoformat(),
                "csrf_header": CSRF_HEADER,
            },
            status_code=200,
        )

    return router


def require_principal(
    store: SessionSource,
    settings: AuthSettings,
    clock: Callable[[], dt.datetime],
) -> Callable[..., Principal]:
    """Build the dependency that turns a cookie into a :class:`Principal`.

    Write methods additionally require the CSRF token. That check lives here
    rather than in a decorator so that a new handler cannot forget it by
    omission: the dependency is the only way to reach a ``Principal``, and the
    session it returns is already the authenticated one.

    ``store`` is a :class:`SessionSource`; the only implementation is
    :class:`kb.access.session.SessionStore`, and that is precisely why this
    survives a gateway restart.
    """

    def dependency(
        request: Request,
        x_kb_csrf: str | None = Header(default=None, alias=CSRF_HEADER),
    ) -> Principal:
        cookie = request.cookies.get(session_cookie_name(settings))
        try:
            session = store.load(cookie, now=clock())
        except SessionMissing as exc:
            # No cookie, an expired cookie and a forged cookie are one answer.
            # Distinguishing them tells an attacker which part of their guess
            # was right.
            raise HTTPException(status_code=401, detail=_BODY_UNAUTHENTICATED) from exc
        if request.method.upper() in CSRF_PROTECTED_METHODS and not csrf_matches(
            session, x_kb_csrf, request.cookies.get(CSRF_COOKIE)
        ):
            _log.warning(
                "auth.csrf.rejected",
                extra={"event": "csrf_rejected", "session_id": str(session.id)},
            )
            raise HTTPException(status_code=403, detail=_BODY_CSRF)
        return session.to_principal()

    return dependency


@contextlib.contextmanager
def principal_scope(conn, principal: Principal) -> Iterator[Any]:
    """Open a transaction with the C06 identity GUCs set.

    Delegates to :func:`kb.access.policy.transaction_identity` rather than
    reissuing the SQL here. The GUCs are transaction-local, so a pooled
    connection that returns from this block answers the next caller as
    nobody.
    """
    with transaction_identity(conn, principal):
        yield conn
