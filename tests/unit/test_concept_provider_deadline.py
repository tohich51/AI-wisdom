"""Regression coverage for the embedding stream's absolute deadline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from kb.research import similarity


def test_embedding_stream_obeys_total_deadline_and_closes(monkeypatch):
    class TrickleStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False
            self.read_wait_started = False
            self.cancelled_while_read_waiting = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            self.read_wait_started = True
            try:
                # Without the overall deadline this remains below the read timeout
                # for each read, never fills a 64 KiB buffer, and runs for >1s.
                for _ in range(150):
                    yield b"x"
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancelled_while_read_waiting = True
                raise
            finally:
                self.closed = True

        async def aclose(self) -> None:
            self.closed = True

    stream = TrickleStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.scheme == "https"
        assert str(request.url) == (similarity.SELECTED_PROFILE.api_base + "/embeddings")
        return httpx.Response(200, stream=stream)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def make_async_client(
        *,
        timeout: httpx.Timeout,
        limits: httpx.Limits,
        follow_redirects: bool,
        trust_env: bool,
    ) -> httpx.AsyncClient:
        client = real_async_client(
            transport=transport,
            timeout=timeout,
            limits=limits,
            follow_redirects=follow_redirects,
            trust_env=trust_env,
        )
        clients.append(client)
        return client

    def reject_sync_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("embedding_provider_used_sync_client")

    monkeypatch.setattr(similarity.httpx, "AsyncClient", make_async_client)
    monkeypatch.setattr(similarity.httpx, "Client", reject_sync_client)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match=r"^embedding_deadline_exceeded$"):
        similarity._openrouter_embeddings(
            ["synthetic concept"],
            "synthetic-provider-key",
            started + 0.05,
        )

    elapsed = time.monotonic() - started
    assert elapsed < 1.0
    assert stream.read_wait_started
    assert stream.cancelled_while_read_waiting
    assert stream.closed
    assert len(clients) == 1
    assert clients[0].is_closed
