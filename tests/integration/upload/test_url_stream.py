"""Real HTTPX streaming lifecycle with a lazy transport; no internet requests."""

from collections.abc import Iterator

import httpx
import pytest

from kb.catalog.upload_fetch import FetchPolicy, UrlRefused, fetch_url

pytestmark = pytest.mark.integration
PUBLIC = "93.184.216.34"


class LazyBody(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], events: list[str], *, fail: bool = False) -> None:
        self.chunks = chunks
        self.events = events
        self.closed = False
        self.fail = fail

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.events.append("body")
            yield chunk
        if self.fail:
            raise httpx.ReadError("private transport detail")

    def close(self) -> None:
        self.closed = True
        self.events.append("close")


class Peer:
    def __init__(self, body: LazyBody, address: str = PUBLIC, *, fail: bool = False) -> None:
        self.body = body
        self.address = address
        self.fail = fail

    def get_extra_info(self, name: str) -> tuple[str, int]:
        assert name == "server_addr"
        self.body.events.append("peer")
        if self.body.closed or self.fail:
            raise OSError(9, "private socket detail")
        return self.address, 443


def response_transport(
    body: LazyBody,
    *,
    address: str = PUBLIC,
    status: int = 200,
    headers: dict[str, str] | None = None,
    peer_fail: bool = False,
) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _: httpx.Response(
            status,
            stream=body,
            headers=headers,
            extensions={"network_stream": Peer(body, address, fail=peer_fail)},
        )
    )


def test_peer_is_checked_while_open_and_before_body() -> None:
    events: list[str] = []
    body = LazyBody([b"hello", b" world"], events)
    result = fetch_url(
        "https://article.example/",
        FetchPolicy(),
        transport=response_transport(body),
        resolver=lambda *_: [PUBLIC],
    )
    assert result.content == b"hello world"
    assert events == ["peer", "body", "body", "close"]
    assert body.closed


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"address": "127.0.0.1"}, "address_not_allowed"),
        ({"address": "8.8.8.8"}, "address_changed"),
        ({"peer_fail": True}, "fetch_failed"),
        ({"status": 404}, "http_error"),
        ({"headers": {"content-length": "100"}}, "too_large"),
        ({"status": 302, "headers": {"location": "http://127.0.0.1/"}}, "address_not_allowed"),
    ],
)
def test_refusal_closes_without_reading_body(options: dict, reason: str) -> None:
    events: list[str] = []
    body = LazyBody([b"never read"], events)
    with pytest.raises(UrlRefused) as error:
        fetch_url(
            "https://article.example/",
            FetchPolicy(max_bytes=10),
            transport=response_transport(body, **options),
            resolver=lambda *_: [PUBLIC],
        )
    assert error.value.reason == reason
    assert "body" not in events
    assert events[-1] == "close"
    assert "private socket detail" not in str(error.value)


@pytest.mark.parametrize("declared", [None, "1", "invalid"])
def test_actual_body_limit_stops_before_remaining_chunks(declared: str | None) -> None:
    events: list[str] = []
    body = LazyBody([b"123", b"456", b"not read"], events)
    headers = {"content-length": declared} if declared is not None else {}
    with pytest.raises(UrlRefused) as error:
        fetch_url(
            "https://article.example/",
            FetchPolicy(max_bytes=5),
            transport=response_transport(body, headers=headers),
            resolver=lambda *_: [PUBLIC],
        )
    assert error.value.reason == "too_large"
    assert events == ["peer", "body", "body", "close"]


def test_mid_body_transport_failure_is_sanitized_and_closed() -> None:
    events: list[str] = []
    body = LazyBody([b"partial"], events, fail=True)
    with pytest.raises(UrlRefused) as error:
        fetch_url(
            "https://article.example/",
            FetchPolicy(),
            transport=response_transport(body),
            resolver=lambda *_: [PUBLIC],
        )
    assert error.value.reason == "fetch_failed"
    assert "private transport detail" not in str(error.value)
    assert events == ["peer", "body", "close"]


@pytest.mark.parametrize("last_answer", [[], ["127.0.0.1"], OSError("DNS detail")])
def test_repeated_dns_validation_cannot_disable_peer_check(
    last_answer: list[str] | OSError,
) -> None:
    calls = 0
    contacted = False

    def resolver(*_: object) -> list[str]:
        nonlocal calls
        calls += 1
        if calls < 3:
            return [PUBLIC]
        if isinstance(last_answer, OSError):
            raise last_answer
        return last_answer

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, content=b"must not read")

    with pytest.raises(UrlRefused) as error:
        fetch_url(
            "https://article.example/",
            FetchPolicy(),
            transport=httpx.MockTransport(handler),
            resolver=resolver,
        )
    assert error.value.reason in {"dns_failure", "address_not_allowed"}
    assert not contacted
