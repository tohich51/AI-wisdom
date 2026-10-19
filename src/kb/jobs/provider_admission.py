from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeAlias
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg import errors

from kb.access.policy import AccessDenied, Principal, transaction_identity

PROVIDER_QUEUE_LOCK = "provider:minimax-owner"

ProviderReviewCode: TypeAlias = Literal["provider_outcome_unknown", "provider_operation_replay"]
FinishState: TypeAlias = Literal["done", "not_started", "unknown"]
JobKind: TypeAlias = Literal["ingest", "research"]


class ProviderBusy(RuntimeError):
    code: Literal["model_unavailable"] = "model_unavailable"


class ProviderReviewRequired(RuntimeError):
    code: ProviderReviewCode

    def __init__(self, code: ProviderReviewCode) -> None:
        super().__init__(code)
        self.code = code


def _reserve_provider_call(
    conn: psycopg.Connection[Any],
    principal: Principal,
    call_key: str,
    token: UUID,
    *,
    job_kind: JobKind,
    job_id: UUID,
    job_attempt: UUID,
) -> None:
    try:
        with transaction_identity(conn, principal):
            conn.execute(
                "SELECT kb.reserve_provider_call(%s,%s,%s,%s,%s)",
                (call_key, token, job_kind, job_id, job_attempt),
            )
    except errors.InsufficientPrivilege as exc:
        if exc.sqlstate == "42501" and exc.diag.message_primary == "forbidden":
            raise AccessDenied("forbidden") from None
        raise
    except errors.CheckViolation as exc:
        if exc.sqlstate == "23514" and exc.diag.message_primary == "provider_busy":
            raise ProviderBusy from None
        if exc.sqlstate == "23514" and exc.diag.message_primary == "provider_operation_replay":
            raise ProviderReviewRequired("provider_operation_replay") from None
        raise


def _finish_provider_call(
    conn: psycopg.Connection[Any],
    principal: Principal,
    call_key: str,
    token: UUID,
    state: FinishState,
) -> None:
    with transaction_identity(conn, principal):
        conn.execute(
            "SELECT kb.finish_provider_call(%s,%s,%s)",
            (call_key, token, state),
        )


def _is_explicit_busy(response: httpx.Response) -> bool:
    if response.status_code != 503:
        return False
    try:
        payload = response.json()
    except (ValueError, httpx.ResponseNotRead):
        return False
    return isinstance(payload, dict) and payload.get("detail") == "busy"


def call_provider(
    conn: psycopg.Connection[Any],
    principal: Principal,
    call_key: str,
    *,
    job_kind: JobKind,
    job_id: UUID,
    job_attempt: UUID,
    send: Callable[[], httpx.Response],
) -> httpx.Response:
    token = uuid4()
    _reserve_provider_call(
        conn,
        principal,
        call_key,
        token,
        job_kind=job_kind,
        job_id=job_id,
        job_attempt=job_attempt,
    )

    try:
        response = send()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _finish_provider_call(conn, principal, call_key, token, "not_started")
        raise ProviderBusy from None
    except httpx.RequestError:
        _finish_provider_call(conn, principal, call_key, token, "unknown")
        raise ProviderReviewRequired("provider_outcome_unknown") from None
    except BaseException:
        _finish_provider_call(conn, principal, call_key, token, "unknown")
        raise

    if _is_explicit_busy(response):
        _finish_provider_call(conn, principal, call_key, token, "not_started")
        raise ProviderBusy

    _finish_provider_call(conn, principal, call_key, token, "done")
    return response
