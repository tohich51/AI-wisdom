"""Exact factory MCP-child compression, retaining quotes and non-MCP routes.

Only MCP payload production is a labelled ASGI double. No gateway lifespan,
SQL, identity provider or network is executed; native MCP is a separate gate.
"""

import gzip
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_operation_registry import REAL_APP_ENV

from kb import gateway

BODY = json.dumps(
    {
        "nodes": [
            {
                "id": str(i),
                "quote": "Синтетическая точная цитата. " * 40,
                "source_id": "11111111-1111-4111-8111-111111111111",
                "fragment_id": "22222222-2222-4222-8222-222222222222",
            }
            for i in range(240)
        ]
    },
    ensure_ascii=False,
).encode()


def factory(monkeypatch, tmp_path, *, event_stream=False):
    for key, value in REAL_APP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KB_BLOB_ROOT", str(tmp_path))
    monkeypatch.setenv("KB_SEMANTIC_SEARCH_ENABLED", "false")
    handed_off = []

    async def child(scope, receive, send):
        assert scope["type"] == "http"
        content_type = b"text/event-stream" if event_stream else b"application/json"
        headers = [(b"content-type", content_type)]
        if not event_stream:
            headers.append((b"content-length", str(len(BODY)).encode()))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        if event_stream:
            await send(
                {"type": "http.response.body", "body": b"data: first\n\n", "more_body": True}
            )
            assert handed_off, "SSE first event must be handed off before the producer finishes"
            await send(
                {"type": "http.response.body", "body": b"data: last\n\n", "more_body": False}
            )
        else:
            await send({"type": "http.response.body", "body": BODY})

    monkeypatch.setattr(gateway, "build_mcp", lambda app, hosts: (SimpleNamespace(), child))
    app = gateway.create_app()

    async def observed(scope, receive, send):
        async def observe(message):
            if message["type"] == "http.response.body":
                handed_off.append(message.get("body", b""))
            await send(message)

        await app(scope, receive, observe)

    return TestClient(observed, base_url="https://gateway.example"), handed_off


@pytest.mark.parametrize("encoding", ["gzip", "identity"])
def test_real_factory_mcp_wire_retains_full_citations(monkeypatch, tmp_path, encoding):
    client, _ = factory(monkeypatch, tmp_path)
    try:
        with client.stream(
            "POST", "/mcp", headers={"Accept-Encoding": encoding}, json={}
        ) as response:
            assert response.status_code == 200
            raw = b"".join(response.iter_raw())
            if encoding == "gzip":
                assert response.headers["content-encoding"] == "gzip"
                assert "accept-encoding" in response.headers["vary"].lower()
                assert int(response.headers["content-length"]) == len(raw)
                assert len(raw) < len(BODY) // 4
                assert gzip.decompress(raw) == BODY
            else:
                assert "content-encoding" not in response.headers
                assert raw == BODY
    finally:
        client.close()


def test_event_stream_first_chunk_is_not_compressed_or_buffered(monkeypatch, tmp_path):
    client, chunks = factory(monkeypatch, tmp_path, event_stream=True)
    try:
        with client.stream(
            "POST", "/mcp", headers={"Accept-Encoding": "gzip"}, json={}
        ) as response:
            assert response.status_code == 200
            assert "content-encoding" not in response.headers
            assert b"".join(response.iter_raw()) == b"data: first\n\ndata: last\n\n"
        assert chunks[0] == b"data: first\n\n"
    finally:
        client.close()


def test_normal_app_shell_is_outside_child_compression(monkeypatch, tmp_path):
    client, _ = factory(monkeypatch, tmp_path)
    try:
        with client.stream(
            "GET", "/app/libraries", headers={"Accept-Encoding": "gzip"}
        ) as response:
            assert response.status_code == 200
            assert "content-encoding" not in response.headers
            body = b"".join(response.iter_raw())
            assert b'id="library-list"' in body
    finally:
        client.close()
