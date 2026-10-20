import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

__all__ = ["MCPTimingMiddleware", "measure"]

_STAGE_NAMES = (
    "transport_auth",
    "call_auth",
    "db_acquire",
    "handler",
    "jsonable_encode",
    "graph_json_dump",
    "network_concepts",
    "network_relations",
    "similarity_ready",
    "similarity_build",
    "similarity_edges",
    "citation_rows",
    "manifest_checks",
)
_STAGES = frozenset(_STAGE_NAMES)
_request_record: ContextVar[dict | None] = ContextVar("kb.mcp.timing.record", default=None)
_logger = logging.getLogger("uvicorn.error")


@contextmanager
def measure(fixed_stage: str):
    record = _request_record.get()
    if fixed_stage not in _STAGES or record is None:
        yield
        return
    started = perf_counter()
    try:
        yield
    finally:
        record["stages"][fixed_stage] += (perf_counter() - started) * 1000.0


def _emit(record):
    try:
        _logger.info(json.dumps(record, separators=(",", ":")))
    except Exception:
        return


class MCPTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") in ("/mcp", "/mcp/")
        ):
            return await self.app(scope, receive, send)

        started = perf_counter()
        record = {
            "event": "mcp_request_timing",
            "total_ms": 0.0,
            "response_status": 0,
            "first_nonempty_body_handoff_ms": 0.0,
            "terminal_body_handoff_ms": 0.0,
            "response_body_bytes": 0,
            "stages": {name: 0.0 for name in _STAGE_NAMES},
        }
        saw_nonempty_body = False
        token = _request_record.set(record)

        async def timed_send(message):
            nonlocal saw_nonempty_body
            await send(message)
            try:
                elapsed_ms = (perf_counter() - started) * 1000.0
                if message.get("type") == "http.response.start":
                    record["response_status"] = message.get("status", 0)
                elif message.get("type") == "http.response.body":
                    body = message.get("body", b"")
                    record["response_body_bytes"] += len(body)
                    if body and not saw_nonempty_body:
                        saw_nonempty_body = True
                        record["first_nonempty_body_handoff_ms"] = elapsed_ms
                    if not message.get("more_body", False):
                        record["terminal_body_handoff_ms"] = elapsed_ms
            except Exception:
                return

        try:
            await self.app(scope, receive, timed_send)
        finally:
            record["total_ms"] = (perf_counter() - started) * 1000.0
            _request_record.reset(token)
            _emit(record)
