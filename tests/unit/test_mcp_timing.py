"""MCP telemetry must preserve messages, errors and request isolation."""

import asyncio
import json
import logging

import anyio
import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from kb.mcp import timing


def _scope(path="/mcp", method="POST"):
    return {"type": "http", "path": path, "method": method}


def _records(caplog):
    return [
        json.loads(item.message)
        for item in caplog.records
        if '"event":"mcp_request_timing"' in item.message
    ]


def test_messages_are_unmodified_and_private_data_never_logged(caplog):
    frames = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"x-private", b"secret-header")],
        },
        {"type": "http.response.body", "body": b"", "more_body": True},
        {"type": "http.response.body", "body": b"secret-citation", "more_body": True},
        {"type": "http.response.body", "body": b"last", "more_body": False},
    ]
    sent = []

    async def app(scope, receive, send):
        for frame in frames:
            await send(frame)

    async def send(message):
        sent.append(message)

    async def receive():
        raise AssertionError("middleware must not consume request data")

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(timing.MCPTimingMiddleware(app)(_scope(), receive, send))
    assert all(a is b for a, b in zip(frames, sent, strict=True))
    (record,) = _records(caplog)
    assert record["response_status"] == 200
    assert record["response_body_bytes"] == len(b"secret-citationlast")
    assert (
        0
        < record["first_nonempty_body_handoff_ms"]
        <= record["terminal_body_handoff_ms"]
        <= record["total_ms"]
    )
    assert "secret-header" not in caplog.text and "secret-citation" not in caplog.text


def test_concurrent_requests_and_thread_stages_are_isolated(caplog):
    async def app(scope, receive, send):
        name = "handler" if scope["case"] == 3 else "graph_json_dump"

        def work():
            with timing.measure(name):
                sum(range(100))

        await anyio.to_thread.run_sync(work)
        await send({"type": "http.response.body", "body": b"x" * scope["case"]})

    async def discard(message):
        pass

    async def receive():
        return {"type": "http.disconnect"}

    async def scenario():
        wrapper = timing.MCPTimingMiddleware(app)
        await asyncio.gather(
            *(wrapper({**_scope(), "case": size}, receive, discard) for size in [3, 7])
        )

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(scenario())
    records = {r["response_body_bytes"]: r for r in _records(caplog)}
    assert set(records) == {3, 7}
    assert records[3]["stages"]["handler"] > 0 and records[3]["stages"]["graph_json_dump"] == 0
    assert records[7]["stages"]["graph_json_dump"] > 0 and records[7]["stages"]["handler"] == 0


def test_logging_failure_cannot_replace_the_original_exception(monkeypatch):
    class OriginalFailure(Exception):
        pass

    async def app(scope, receive, send):
        with timing.measure("handler"):
            raise OriginalFailure("original")

    def failed_log(*args, **kwargs):
        raise RuntimeError("logging failed")

    monkeypatch.setattr(timing._logger, "info", failed_log)
    with pytest.raises(OriginalFailure, match="original"):
        asyncio.run(timing.MCPTimingMiddleware(app)(_scope(), None, None))


def test_other_routes_are_untouched_and_context_is_reset(caplog):
    async def app(scope, receive, send):
        with timing.measure("handler"):
            await send({"type": "http.response.body", "body": b"x"})

    async def discard(message):
        pass

    async def scenario():
        wrapper = timing.MCPTimingMiddleware(app)
        await wrapper(_scope(), None, discard)
        await wrapper(_scope("/other/private", "POST"), None, discard)
        await wrapper(_scope("/mcp", "GET"), None, discard)
        with timing.measure("arbitrary_private_name"):
            pass

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        asyncio.run(scenario())
    (record,) = _records(caplog)
    assert set(record["stages"]) == set(timing._STAGE_NAMES)
    assert record["response_body_bytes"] == 1
    assert "arbitrary_private_name" not in caplog.text


def test_official_mcp_transport_preserves_thread_stage_context(caplog):
    server = MCPServer("synthetic-timing")

    @server.tool()
    def measured() -> str:
        with timing.measure("handler"):
            return "synthetic evidence"

    child = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["mcp.example.test"],
            allowed_origins=["http://mcp.example.test"],
        ),
    )

    async def scenario():
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=timing.MCPTimingMiddleware(child)),
                base_url="http://mcp.example.test",
                headers={"Accept": "application/json, text/event-stream"},
            ) as client:
                initialized = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "synthetic-test", "version": "1"},
                        },
                    },
                )
                assert initialized.status_code == 200
                version = initialized.json()["result"]["protocolVersion"]
                result = await client.post(
                    "/mcp",
                    headers={"MCP-Protocol-Version": version},
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "measured", "arguments": {}},
                    },
                )
                assert result.status_code == 200
                assert result.json()["result"]["content"][0]["text"] == "synthetic evidence"
                return len(result.content)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        size = asyncio.run(scenario())
    measured_records = [r for r in _records(caplog) if r["stages"]["handler"] > 0]
    (record,) = measured_records
    assert record["response_body_bytes"] == size
    assert "synthetic evidence" not in caplog.text
