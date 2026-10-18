"""One-use browser pairing and standard OAuth token exchange for agent sessions."""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Final, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from kb.access.bearer_policy import bearer_principal, require_scope
from kb.access.identity import MAX_TOKEN_CHARS, IdentityRejected
from kb.access.policy import AccessDenied, Principal
from kb.access.session import BrowserPairingBusy, SessionMissing, SessionStore
from kb.http.auth_config import AuthSettings, safe_return_target
from kb.http.auth_routes import session_cookie_name, set_session_cookies
from kb.runtime_identity import assert_active_membership

_PAIRING_TTL_SECONDS: Final[int] = 120
_MAX_BODY_BYTES: Final[int] = 4096
_MAX_RETURN_TARGET: Final[int] = 2048
_BINDING_COOKIE: Final[str] = "kb_agent_binding"
_SUPPORTED_PRODUCT_SCOPES: Final[frozenset[str]] = frozenset(
    {"kb:read", "kb:contribute", "kb:experience", "kb:export", "kb:admin"}
)
_EXCHANGE_GRANT: Final[str] = "urn:ietf:params:oauth:grant-type:token-exchange"
_ACCESS_KIND: Final[str] = "urn:ietf:params:oauth:token-type:access_token"
_ID_KIND: Final[str] = "urn:ietf:params:oauth:token-type:id_token"
_ORIGIN_STATE: Final[str] = "_kb_browser_pairing_origin"
_STATIC_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "static"

_SECURITY_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


class _BeginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    return_to: str | None = Field(default=None, max_length=_MAX_RETURN_TARGET)

    @field_validator("return_to")
    @classmethod
    def validate_return_to(cls, value: str | None) -> str | None:
        target, valid = _safe_bounded_return(value)
        if not valid:
            raise ValueError("invalid_return_target")
        return target


class _PairingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairing_id: UUID
    nonce: str = Field(min_length=16, max_length=256)


class _CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairing_id: UUID


class _ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str = Field(min_length=16, max_length=256)


class _InvalidRequest(Exception):
    pass


class _LiveSession(Exception):
    pass


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _binding_cookie_name(settings: AuthSettings) -> str:
    return f"__Host-{_BINDING_COOKIE}" if settings.cookie_secure else _BINDING_COOKIE


def _safe_bounded_return(raw: str | None) -> tuple[str | None, bool]:
    if raw is None:
        return None, True
    if len(raw) > _MAX_RETURN_TARGET:
        return None, False
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return None, False
    target = safe_return_target(raw)
    if target is None:
        return None, not raw.strip()
    return target, True


def _redirect_origin(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("invalid_redirect_origin")
    if parts.username is not None or parts.password is not None:
        raise ValueError("invalid_redirect_origin")
    host = parts.hostname.lower()
    if any(char.isspace() or ord(char) < 32 for char in host):
        raise ValueError("invalid_redirect_origin")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid_redirect_origin") from exc
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    scheme = parts.scheme.lower()
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _strict_origin(request: Request, expected: str) -> bool:
    values = request.headers.getlist("origin")
    return len(values) == 1 and len(values[0]) <= 512 and values[0] == expected


def _secured(response: Response) -> Response:
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def _json_response(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    _secured(response)
    return response


def _error(code: str, status_code: int) -> JSONResponse:
    return _json_response({"error": code}, status_code)


def _expire_binding(response: Response, settings: AuthSettings) -> Response:
    response.delete_cookie(_binding_cookie_name(settings), path="/")
    return response


async def _read_model(request: Request, model: type[_ModelT]) -> _ModelT:
    content_types = request.headers.getlist("content-type")
    if (
        len(content_types) != 1
        or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
    ):
        raise _InvalidRequest
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise _InvalidRequest
    if lengths:
        try:
            length = int(lengths[0])
        except ValueError as exc:
            raise _InvalidRequest from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise _InvalidRequest
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY_BYTES:
            raise _InvalidRequest
        body.extend(chunk)
    try:
        document = json.loads(bytes(body))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _InvalidRequest from exc
    if not isinstance(document, dict):
        raise _InvalidRequest
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        raise _InvalidRequest from exc


def _build_auth_router(
    store: SessionStore,
    settings: AuthSettings,
    expected_origin: str,
) -> APIRouter:
    router = APIRouter(tags=["browser-pairing"])

    @router.get("/auth/agent", response_model=None)
    async def agent_page(request: Request) -> Response:
        values = request.query_params.getlist("return_to")
        if len(values) > 1:
            return _error("invalid_return_target", 400)
        _, valid = _safe_bounded_return(values[0] if values else None)
        if not valid:
            return _error("invalid_return_target", 400)
        return _secured(FileResponse(_STATIC_DIR / "agent-login.html", media_type="text/html"))

    @router.post("/auth/agent/begin", response_model=None)
    async def begin(request: Request) -> Response:
        if not _strict_origin(request, expected_origin):
            return _error("origin_forbidden", 403)
        try:
            body = await _read_model(request, _BeginBody)
        except _InvalidRequest:
            return _error("invalid_request", 400)
        now = _utcnow()
        session_cookie = request.cookies.get(session_cookie_name(settings))

        def start() -> Any:
            if session_cookie is not None:
                try:
                    store.load(session_cookie, now=now)
                except SessionMissing:
                    pass
                else:
                    raise _LiveSession
            return store.begin_browser_pairing(return_to=body.return_to, now=now)

        try:
            pairing = await run_in_threadpool(start)
        except BrowserPairingBusy:
            response = _error("browser_pairing_busy", 429)
            response.headers["Retry-After"] = "60"
            return response
        except _LiveSession:
            return _error("browser_login_session_exists", 409)
        response = _json_response(
            {
                "pairing_id": str(pairing.id),
                "nonce": pairing.nonce,
                "expires_at": pairing.expires_at.isoformat(),
            },
            201,
        )
        response.set_cookie(
            _binding_cookie_name(settings),
            pairing.binding,
            max_age=_PAIRING_TTL_SECONDS,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return response

    @router.post("/auth/agent/complete", response_model=None)
    async def complete(request: Request) -> Response:
        if not _strict_origin(request, expected_origin):
            return _error("origin_forbidden", 403)
        try:
            body = await _read_model(request, _PairingBody)
        except _InvalidRequest:
            return _expire_binding(_error("invalid_request", 400), settings)
        now = _utcnow()
        binding = request.cookies.get(_binding_cookie_name(settings))
        if not binding:
            return _error("browser_binding_required", 403)

        def consume() -> Any:
            return store.consume_browser_pairing(
                body.pairing_id,
                body.nonce,
                binding=binding,
                now=now,
            )

        try:
            approval = await run_in_threadpool(consume)
        except SessionMissing:
            return _expire_binding(_error("agent_login_denied", 403), settings)
        if approval is None:
            return _json_response({"status": "pending"}, 202)

        principal = Principal(
            approval.identity.principal_id,
            approval.identity.account_id,
            cloud_client=approval.authority_cloud_client,
        )

        def check_membership() -> Any:
            return assert_active_membership(
                request.app.state.db_connection,
                approval.identity,
                principal,
            )

        try:
            membership = await run_in_threadpool(check_membership)
        except Exception:
            membership = False
        if membership is False:
            return _expire_binding(_error("agent_login_denied", 403), settings)

        def issue() -> Any:
            return store.create_session(
                approval.identity,
                now=now,
                authority_scopes=approval.authority_scopes,
                authority_cloud_client=approval.authority_cloud_client,
                credential_expires_at=approval.credential_expires_at,
            )

        try:
            issued = await run_in_threadpool(issue)
        except SessionMissing:
            return _expire_binding(_error("provider_session_logged_out", 400), settings)
        except ValueError:
            return _expire_binding(_error("credential_expired", 400), settings)
        response = _json_response({"authenticated": True, "safe_return_to": approval.return_to})
        _expire_binding(response, settings)
        set_session_cookies(response, issued, settings)
        return response

    @router.post("/auth/agent/cancel", response_model=None)
    async def cancel(request: Request) -> Response:
        if not _strict_origin(request, expected_origin):
            return _error("origin_forbidden", 403)
        try:
            body = await _read_model(request, _CancelBody)
        except _InvalidRequest:
            return _expire_binding(_error("invalid_request", 400), settings)
        now = _utcnow()
        binding = request.cookies.get(_binding_cookie_name(settings))
        if not binding:
            return _error("browser_binding_required", 403)
        cancelled = await run_in_threadpool(
            store.cancel_browser_pairing,
            body.pairing_id,
            binding=binding,
            now=now,
        )
        if cancelled:
            response = _json_response({"status": "cancelled"})
        else:
            response = _error("browser_login_not_cancelled", 400)
        return _expire_binding(response, settings)

    return router


approval_router = APIRouter(tags=["browser-pairing"])


@approval_router.post("/browser-logins/{pairing_id}/approve", response_model=None)
async def approve_browser_login(request: Request, pairing_id: str) -> Response:
    expected_origin = getattr(request.app.state, _ORIGIN_STATE, None)
    if not isinstance(expected_origin, str) or not _strict_origin(request, expected_origin):
        return _error("origin_forbidden", 403)
    token = _bearer_token(request.headers.getlist("authorization"))
    if token is None:
        return _error("not_authenticated", 401)
    try:
        parsed_id = UUID(pairing_id)
    except (ValueError, AttributeError, TypeError):
        return _error("invalid_browser_login_id", 400)
    try:
        body = await _read_model(request, _ApprovalBody)
    except _InvalidRequest:
        return _error("invalid_request", 400)
    origin = request.headers.getlist("origin")[0]
    try:
        approved = await run_in_threadpool(
            request.app.state.approve_browser_login,
            token,
            parsed_id,
            body.nonce,
            origin,
        )
    except Exception:
        approved = False
    if not approved:
        return _error("browser_login_approval_denied", 403)
    return _json_response({"status": "approved"})


def _bearer_token(values: list[str]) -> str | None:
    if len(values) != 1:
        return None
    parts = values[0].split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    token = parts[1]
    if any(char.isspace() for char in token) or len(token) > MAX_TOKEN_CHARS:
        return None
    return token


def _verified_claims(token: str) -> dict[str, Any]:
    claims = jwt.decode(token, key="", options={"verify_signature": False})
    if not isinstance(claims, dict):
        raise IdentityRejected("malformed_token")
    return claims


def _amr(claims: dict[str, Any]) -> frozenset[str] | None:
    value = claims.get("amr")
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return frozenset(value)


def _assurance_matches(original_claims: dict[str, Any], exchanged_claims: dict[str, Any]) -> bool:
    original_acr = original_claims.get("acr")
    if not isinstance(original_acr, str) or not original_acr.strip():
        return False
    if exchanged_claims.get("acr") != original_acr:
        return False
    original_methods = _amr(original_claims)
    exchanged_methods = _amr(exchanged_claims)
    if original_methods is not None and (
        exchanged_methods is None or not original_methods.issubset(exchanged_methods)
    ):
        return False
    if "amr" in original_claims and original_methods is None:
        return False
    if "auth_time" in original_claims or "auth_time" in exchanged_claims:
        original_time = original_claims.get("auth_time")
        exchanged_time = exchanged_claims.get("auth_time")
        if (
            isinstance(original_time, bool)
            or isinstance(exchanged_time, bool)
            or not isinstance(original_time, (int, float))
            or not isinstance(exchanged_time, (int, float))
            or original_time != exchanged_time
        ):
            return False
    return True


def _approve_browser_login(
    app: Any,
    store: SessionStore,
    settings: AuthSettings,
    http_client: httpx.Client,
    browser_verifier: Any,
    expected_origin: str,
    token: str,
    pairing_id: UUID,
    nonce: str,
    origin: str,
) -> bool:
    if (
        not isinstance(token, str)
        or not token
        or len(token) > MAX_TOKEN_CHARS
        or not isinstance(pairing_id, UUID)
        or not isinstance(nonce, str)
        or not 16 <= len(nonce) <= 256
        or not isinstance(origin, str)
        or origin != expected_origin
    ):
        return False
    try:
        original = app.state.resolve_token(token)
        require_scope(original, "kb:read")
    except (
        IdentityRejected,
        AccessDenied,
        SessionMissing,
        HTTPException,
        AttributeError,
        TypeError,
        ValueError,
    ):
        return False
    local_clients = frozenset(os.environ.get("KB_MCP_LOCAL_CLIENT_IDS", "").split())
    client_id = getattr(original, "client_id", None)
    if not isinstance(client_id, str) or client_id not in local_clients:
        return False
    original_sid = getattr(original, "provider_session_id", None)
    if not isinstance(original_sid, str) or not original_sid:
        return False
    original_product = frozenset(original.scopes) & _SUPPORTED_PRODUCT_SCOPES
    if "kb:read" not in original_product:
        return False
    original_principal = bearer_principal(original)
    try:
        endpoint = browser_verifier.endpoint("token_endpoint")
    except (IdentityRejected, TypeError, ValueError):
        return False
    requested_scopes = sorted(original_product)
    try:
        response = http_client.post(
            endpoint,
            data={
                "grant_type": _EXCHANGE_GRANT,
                "subject_token": token,
                "subject_token_type": _ACCESS_KIND,
                "requested_token_type": _ID_KIND,
                "audience": settings.client_id,
                "scope": "openid " + " ".join(requested_scopes),
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
            },
            headers={"accept": "application/json"},
        )
    except (httpx.HTTPError, TypeError, ValueError):
        return False
    if response.status_code != 200:
        return False
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("issued_token_type") != _ID_KIND:
        return False
    if "refresh_token" in payload:
        return False
    id_token = payload.get("access_token")
    if not isinstance(id_token, str) or not id_token:
        return False
    try:
        exchanged = browser_verifier.verify(id_token, require_scopes=False)
    except (IdentityRejected, TypeError, ValueError):
        return False
    if (
        exchanged.issuer != original.issuer
        or exchanged.subject != original.subject
        or exchanged.principal_id != original.principal_id
        or exchanged.account_id != original.account_id
        or not isinstance(exchanged.provider_session_id, str)
        or not exchanged.provider_session_id
        or exchanged.provider_session_id != original_sid
    ):
        return False
    try:
        original_claims = _verified_claims(token)
        exchanged_claims = _verified_claims(id_token)
    except (jwt.PyJWTError, IdentityRejected, TypeError, ValueError):
        return False
    if not _assurance_matches(original_claims, exchanged_claims):
        return False
    raw_scope = payload.get("scope")
    if not isinstance(raw_scope, str):
        return False
    granted_scopes = frozenset(raw_scope.split())
    granted_product = granted_scopes & _SUPPORTED_PRODUCT_SCOPES
    if "kb:read" not in granted_product or not granted_product.issubset(original_product):
        return False
    ceiling = original_product & granted_scopes
    # ID tokens need not carry scope. The AS exchange response supplies it;
    # retain only the intersection in the already signature-verified identity.
    exchanged = replace(exchanged, scopes=ceiling)
    credential_expires_at = min(original.expires_at, exchanged.expires_at)
    if credential_expires_at <= _utcnow():
        return False
    try:
        return bool(
            store.approve_browser_pairing(
                pairing_id,
                nonce,
                exchanged,
                authority_scopes=ceiling,
                authority_cloud_client=original_principal.cloud_client,
                credential_expires_at=credential_expires_at,
                now=_utcnow(),
            )
        )
    except Exception:
        return False


def install_browser_pairing(
    app: Any,
    store: SessionStore,
    settings: AuthSettings,
    http_client: httpx.Client,
    browser_verifier: Any,
) -> None:
    expected_origin = _redirect_origin(settings.redirect_uri)

    def approve_browser_login(
        token: str,
        pairing_id: UUID,
        nonce: str,
        origin: str,
    ) -> bool:
        return _approve_browser_login(
            app,
            store,
            settings,
            http_client,
            browser_verifier,
            expected_origin,
            token,
            pairing_id,
            nonce,
            origin,
        )

    app.state.approve_browser_login = approve_browser_login
    setattr(app.state, _ORIGIN_STATE, expected_origin)
    app.include_router(_build_auth_router(store, settings, expected_origin))
