"""IP pinning: counted local HTTP fixture and labelled request doubles."""

from __future__ import annotations

import http.server
import socket
import threading

import httpx
import pytest

from kb.catalog.upload_fetch import FetchPolicy, UrlRefused, fetch_url

APPROVED_IP = "93.184.216.34"


class _CounterHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.server.hits.append(self.path)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args, **kwargs) -> None:  # silence the test log
        return


def _serve_once():
    """A loopback HTTP server that counts the GETs it actually receives."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _CounterHandler)
    server.hits = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_a_rebound_name_never_reaches_the_far_side(monkeypatch):
    """A name that passes policy but re-resolves to 127.0.0.1 must send nothing."""
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    server = _serve_once()
    targets: list[object] = []
    real_create_connection = socket.create_connection

    def fake_create_connection(address, *args, **kwargs):
        targets.append(address[0])
        if address[0] == APPROVED_IP:
            raise OSError("approved public connection blocked by the test; no WAN")
        if address[0] == "public.test":
            return real_create_connection(("127.0.0.1", server.server_port), *args, **kwargs)
        raise OSError("unexpected connection blocked by the test")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(
        "kb.catalog.upload_fetch.default_resolver", lambda host, port: [APPROVED_IP]
    )

    try:
        with pytest.raises(UrlRefused) as refusal:
            fetch_url("http://public.test/some/path?q=1", FetchPolicy())
    finally:
        server.shutdown()
        server.server_close()

    assert server.hits == [], "a GET reached the rebound internal HTTP fixture"
    assert targets == [APPROVED_IP], "must connect to the approved address, not the name"
    assert refusal.value.reason == "fetch_failed"


@pytest.mark.parametrize(
    "url,approved,host_header,sni",
    [
        (
            "https://original.example:8443/path/to.bin?q=x#frag",
            APPROVED_IP,
            "original.example:8443",
            "original.example",
        ),
        (
            "https://xn--80ak6aa92e.example/deep/path?a=1&b=2",
            APPROVED_IP,
            "xn--80ak6aa92e.example",
            "xn--80ak6aa92e.example",
        ),
        (
            "https://[2606:2800:220:1:248:1893:25c8:1946]:9443/v6/path",
            "2606:2800:220:1:248:1893:25c8:1946",
            "[2606:2800:220:1:248:1893:25c8:1946]:9443",
            "2606:2800:220:1:248:1893:25c8:1946",
        ),
    ],
)
def test_the_request_carries_the_approved_address(monkeypatch, url, approved, host_header, sni):
    """The pinned request keeps the name for TLS/Host and the IP for routing."""
    seen: list[httpx.Request] = []

    def recording_handle(self, request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf"},
            content=b"%PDF-1.7\n",
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", recording_handle)
    monkeypatch.setattr("kb.catalog.upload_fetch.default_resolver", lambda host, port: [approved])
    # An untrusted proxy environment must not be able to resolve the name itself.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    fetched = fetch_url(url, FetchPolicy())

    assert len(seen) == 1
    request = seen[0]
    assert request.url.host == approved, "the transport must be given the approved IP"
    assert request.headers["host"] == host_header
    assert request.extensions.get("sni_hostname") == sni
    assert request.url.path == httpx.URL(url).path
    assert request.url.query == httpx.URL(url).query
    assert fetched.final_url == url, "the caller still sees the URL it asked for"
    assert fetched.content == b"%PDF-1.7\n"
    assert fetched.media_type == "application/pdf"
    assert fetched.approved_addresses == (approved,)
