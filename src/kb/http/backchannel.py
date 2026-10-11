import datetime as dt
from collections.abc import Callable
from urllib.parse import parse_qs

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from kb.access.identity import IdentityRejected
from kb.access.logout import LogoutVerifier
from kb.access.session import SessionStore

_MAX_BODY_BYTES = 32768
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


def _json_response(content: dict[str, str], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content=content,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _bad_request() -> JSONResponse:
    return _json_response({"error": "invalid_request"}, status_code=400)


def create_backchannel_router(
    *,
    verifier: LogoutVerifier,
    store: SessionStore,
    clock: Callable[[], dt.datetime],
) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"])

    @router.post("/backchannel-logout")
    async def endpoint(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != _FORM_CONTENT_TYPE:
            return _bad_request()

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_BODY_BYTES:
                return _bad_request()
            body.extend(chunk)

        try:
            form = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                max_num_fields=32,
            )
        except (UnicodeDecodeError, ValueError):
            return _bad_request()

        tokens = form.get("logout_token", [])
        if len(tokens) != 1 or not tokens[0]:
            return _bad_request()

        token = tokens[0]
        now = clock()
        try:
            verified_logout = await run_in_threadpool(verifier.verify, token, now=now)
        except IdentityRejected:
            return _json_response(
                {"error": "invalid_logout_token"},
                status_code=400,
            )

        await run_in_threadpool(store.apply_provider_logout, verified_logout, now=now)
        return _json_response({"status": "signed_out"})

    return router
