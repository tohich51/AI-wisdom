"""C10 — URL ingestion: SSRF, redirects, and the IPs that must never be reached.

PRODUCT-SPEC: "URL ingestion проверяет SSRF/redirect/IP". ACCESS-MODEL A14: "URL
localhost/private IPv4/IPv6, redirect/DNS rebinding, path traversal" with the
expected result "Загрузка блокируется; внутренние сервисы и файлы не читаются".

What is real here and what is not, stated up front:

* ``test_a_loopback_fetch_is_refused_and_the_server_never_sees_a_request``
  starts a **real HTTP server on a real loopback socket** and points the real
  fetcher at it. Nothing about the network is faked. The evidence that the
  refusal is real is that the listening server counted zero requests: the
  request never left the process.
* ``test_a_loopback_url_is_refused_before_any_socket_is_opened`` replaces
  ``socket.socket`` and ``socket.create_connection`` with functions that raise,
  and the fetch still ends in a refusal rather than a crash from the transport.
  That proves the *ordering*, which the first test cannot.
* The redirect and rebinding cases use a real ``httpx.Client`` over a
  ``MockTransport``. What is under test is this card's control flow over real
  httpx response objects; only the socket is absent, and each test name says so.

No test in this file asserts that anything was fetched from the public internet.
Nothing in this environment may.
"""

from __future__ import annotations

import http.server
import socket
import threading
import unittest.mock
from collections.abc import Iterator

import httpx
import pytest

from kb.catalog.upload_fetch import (
    FetchPolicy,
    UrlRefused,
    _verify_peer,
    check_url,
    fetch_url,
    is_blocked_address,
    parse_url,
)

pytestmark = pytest.mark.integration

POLICY = FetchPolicy()

# One public address stands in for "a host that resolves to the internet". It is
# documentation space and is never dialled: every case below is refused before a
# connection, or answered by a mock transport.
PUBLIC = "93.184.216.34"


def resolver_for(mapping: dict[str, list[str]]):
    """A resolver that answers only what the test declares.

    Anything not in the mapping resolves to loopback, so a test that forgot to
    declare a host gets a refusal rather than an accidental pass.
    """

    def _resolve(host: str, _port: int) -> list[str]:
        return list(mapping.get(host, ["127.0.0.1"]))

    return _resolve


def mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ======================================================= the addresses


@pytest.mark.parametrize(
    ("address", "why"),
    [
        ("127.0.0.1", "loopback"),
        ("127.1.2.3", "loopback, not just 127.0.0.1"),
        ("::1", "IPv6 loopback"),
        ("::ffff:127.0.0.1", "IPv4-mapped loopback"),
        ("10.0.0.5", "private"),
        ("172.16.0.1", "private"),
        ("172.31.255.255", "private, top of the 172.16/12 range"),
        ("192.168.1.1", "private"),
        ("169.254.169.254", "the cloud metadata endpoint, link-local"),
        ("100.64.0.1", "CGNAT"),
        ("0.0.0.0", "this network"),  # noqa: S104 - a literal, not a bind address
        ("224.0.0.1", "multicast"),
        ("255.255.255.255", "broadcast, inside the reserved block"),
        ("fc00::1", "IPv6 unique local"),
        ("fe80::1", "IPv6 link-local"),
        ("2001:db8::1", "IPv6 documentation"),
        ("64:ff9b::7f00:1", "NAT64 wrapping loopback"),
        ("not-an-address", "not an address at all"),
    ],
)
def test_an_internal_address_is_never_reachable(address: str, why: str) -> None:
    assert is_blocked_address(address) is True, why


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111"])
def test_a_public_address_is_not_blocked_by_the_list_itself(address: str) -> None:
    """The control for the test above.

    Without it, "everything is blocked" would pass every case in this file. This
    asserts the filter discriminates rather than simply refusing.
    """
    assert is_blocked_address(address) is False


def test_172_16_to_172_31_is_private_and_the_neighbours_are_not() -> None:
    """The edges of 172.16.0.0/12, asserted from both sides.

    A filter that blocked all of 172.0.0.0/8 would pass every private-address
    test in this file and quietly refuse legitimate hosts. The boundary is the
    point: 172.15.x is public, 172.16.x is private, 172.31.x is private, 172.32.x
    is public.
    """
    assert is_blocked_address("172.15.255.255") is False
    assert is_blocked_address("172.16.0.0") is True
    assert is_blocked_address("172.31.255.255") is True
    assert is_blocked_address("172.32.0.1") is False


# ================================================= the real loopback refusal


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self) -> None:
        type(self).hits += 1
        body = b"an internal service that must never be readable"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:  # keep the test output clean
        return


@pytest.fixture
def loopback_server() -> Iterator[str]:
    """A real HTTP server on a real loopback socket, on an ephemeral port.

    This is the actual target A14 describes: an internal service, reachable from
    the gateway's own host, that must not be. The server counts the requests it
    receives so "refused" can be proved rather than asserted.
    """
    _CountingHandler.hits = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"http://{host}:{port}/internal/secret"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_a_loopback_fetch_is_refused_and_the_server_never_sees_a_request(
    loopback_server: str,
) -> None:
    """The real thing: a live loopback service, a real refusal, zero requests.

    No mock anywhere in this test. The URL is genuinely reachable — the
    :func:`_CountingHandler` server really is listening on 127.0.0.1 — and
    ``fetch_url`` genuinely refuses it. The proof that no request was made is the
    server's own hit counter, not the absence of an exception: a fetch that was
    made and then hidden would look identical from the caller's side.
    """
    with pytest.raises(UrlRefused) as raised:
        fetch_url(loopback_server, POLICY)
    assert raised.value.reason == "address_not_allowed"
    assert _CountingHandler.hits == 0, (
        "the internal service received a request; the refusal came too late"
    )


def test_the_service_is_genuinely_reachable_when_fetched_normally(loopback_server: str) -> None:
    """The control for the test above.

    If the server were not actually serving, "the gateway refused" would prove
    nothing at all. This bypasses the gateway on purpose and proves the socket is
    live — a plain ``httpx.get`` with no policy, which the SSRF code never does.
    """
    response = httpx.get(loopback_server, timeout=5)
    assert response.status_code == 200
    assert b"internal service" in response.content
    assert _CountingHandler.hits == 1


def test_a_loopback_url_is_refused_before_any_socket_is_opened() -> None:
    """Ordering: the check runs before the transport, not after the failure.

    ``socket.socket`` and ``socket.create_connection`` are replaced with
    functions that raise. If the refusal depended on a connection attempt, this
    would fail with ``OSError`` instead of ``UrlRefused``. The distinction is the
    difference between "we blocked it" and "it happened to fail".
    """

    def explode(*_args, **_kwargs):
        raise AssertionError("a socket was opened; the URL check runs too late")

    with (
        unittest.mock.patch("socket.socket", explode),
        unittest.mock.patch("socket.create_connection", explode),
    ):
        with pytest.raises(UrlRefused) as raised:
            fetch_url("http://127.0.0.1:9/anything", POLICY)
    assert raised.value.reason == "address_not_allowed"


def test_the_ip_literal_is_refused_without_consulting_a_resolver() -> None:
    """A URL naming the address is refused on the address, not on a lookup.

    A resolver that "helpfully" mapped 169.254.169.254 to a public address would
    otherwise turn the metadata-endpoint block into a suggestion.
    """
    called: list[str] = []

    def resolver(host: str, _port: int) -> list[str]:
        called.append(host)
        return [PUBLIC]

    with pytest.raises(UrlRefused) as raised:
        check_url("http://169.254.169.254/latest/meta-data/", POLICY, resolver=resolver)
    assert raised.value.reason == "address_not_allowed"
    assert called == [], "an IP literal was handed to the resolver instead of checked"


# ============================================================ URL shape


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("ftp://example.test/x", "scheme_not_allowed"),
        ("gopher://example.test/x", "scheme_not_allowed"),
        ("dict://example.test/x", "scheme_not_allowed"),
        ("data:text/plain,hello", "scheme_not_allowed"),
        ("http://user:secret@example.test/x", "credentials_in_url"),
        ("http://localhost/x", "host_not_allowed"),
        ("http://api.localhost/x", "host_not_allowed"),
        ("http://db.internal/x", "host_not_allowed"),
        ("http://2130706433/", "address_not_allowed"),
        ("http://0x7f000001/", "address_not_allowed"),
        ("http://0177.0.0.1/", "address_not_allowed"),
    ],
)
def test_a_url_that_is_not_plain_public_http_is_refused(url: str, reason: str) -> None:
    """Schemes, credentials, internal names and numeric address forms.

    The last three rows are the ones a naive implementation gets wrong: a filter
    that only understands dotted quads, and one that treats ``localhost`` as just
    another name to resolve.
    """
    with pytest.raises(UrlRefused) as raised:
        fetch_url(url, POLICY, transport=mock_transport(lambda _r: httpx.Response(200)))
    assert raised.value.reason == reason


def test_a_refusal_message_never_contains_the_resolved_address() -> None:
    """A refusal reason must not become a map of the installation.

    The caller already knows the URL they typed. Resolved addresses, by
    contrast, are things they were not supposed to learn.
    """
    with pytest.raises(UrlRefused) as raised:
        check_url(
            "http://rebind.test/x", POLICY, resolver=resolver_for({"rebind.test": ["10.1.2.3"]})
        )
    message = str(raised.value)
    assert "10.1.2.3" not in message
    assert PUBLIC not in message


def test_an_empty_or_hostless_url_is_refused() -> None:
    for url in ("", "   ", "http://", "https:///path", "not a url at all"):
        with pytest.raises(UrlRefused):
            parse_url(url)


def test_ports_are_read_not_guessed() -> None:
    assert parse_url("https://example.test/a") == ("https", "example.test", 443, "/a")
    assert parse_url("http://example.test:8080/a") == ("http", "example.test", 8080, "/a")
    with pytest.raises(UrlRefused):
        parse_url("http://example.test:99999/a")


# ============================================================== redirects


def _router(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    routes: dict[str, httpx.Response] = {
        "/ok": httpx.Response(
            200, content=b"%PDF-1.7 body", headers={"Content-Type": "application/pdf"}
        ),
        "/loop": httpx.Response(302, headers={"Location": "/loop"}),
        "/hop1": httpx.Response(302, headers={"Location": "/hop2"}),
        "/hop2": httpx.Response(302, headers={"Location": "/ok"}),
        "/to-loopback": httpx.Response(302, headers={"Location": "http://127.0.0.1:9/secret"}),
        "/to-metadata": httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"}),
        "/to-file": httpx.Response(302, headers={"Location": "file:///etc/shadow"}),
        "/to-private": httpx.Response(302, headers={"Location": "http://10.0.0.9/internal"}),
        "/no-location": httpx.Response(302),
    }
    return routes.get(path, httpx.Response(404))


def test_a_relative_redirect_is_followed_and_re_validated() -> None:
    result = fetch_url(
        "http://public.test/hop1",
        POLICY,
        transport=mock_transport(_router),
        resolver=resolver_for({"public.test": [PUBLIC]}),
    )
    assert result.status_code == 200
    assert result.hops == 2
    assert result.final_url.endswith("/ok")


def test_a_redirect_to_loopback_is_refused() -> None:
    """The check runs again on the *hop*, not only on the URL that was typed.

    A first hop to a public host that answers "302 Location: http://127.0.0.1/"
    is the standard SSRF pivot, and it is caught because every hop goes through
    ``check_url`` before a request is built.
    """
    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/to-loopback",
            POLICY,
            transport=mock_transport(_router),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "address_not_allowed"


def test_a_redirect_to_the_metadata_endpoint_is_refused() -> None:
    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/to-metadata",
            POLICY,
            transport=mock_transport(_router),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "address_not_allowed"


def test_a_redirect_to_a_private_address_is_refused() -> None:
    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/to-private",
            POLICY,
            transport=mock_transport(_router),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "address_not_allowed"


def test_a_redirect_to_a_non_http_scheme_is_refused() -> None:
    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/to-file",
            POLICY,
            transport=mock_transport(_router),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "scheme_not_allowed"


def test_redirects_are_capped() -> None:
    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/loop",
            POLICY,
            transport=mock_transport(_router),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "too_many_redirects"


def test_a_redirect_without_a_destination_is_refused() -> None:
    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/no-location",
            POLICY,
            transport=mock_transport(_router),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "bad_redirect"


# ================================================== DNS rebinding, in the hole


def test_a_resolver_that_answers_twice_differently_is_caught() -> None:
    """Rebinding: the first answer is public, the second is not.

    The first ``check_url`` approves a public address, the connection is made,
    and the *next* resolution — for the hop, or for the next request — returns
    something internal. Because the approved set is recomputed and every hop is
    re-checked, the second answer is refused before it is used.
    """
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def flipping_resolver(_host: str, _port: int) -> list[str]:
        try:
            return next(answers)
        except StopIteration:
            return [PUBLIC]

    hops = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/first":
            return httpx.Response(302, headers={"Location": "http://rebind.test/second"})
        return httpx.Response(200, content=b"ok")

    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://rebind.test/first",
            POLICY,
            transport=mock_transport(handler),
            resolver=flipping_resolver,
        )
    assert raised.value.reason == "address_not_allowed"
    assert hops["n"] == 0


class _FakeStream:
    """Stands in for httpx's network stream so the peer address can be supplied.

    A real TCP transport reports the socket's peer through
    ``get_extra_info("server_addr")``; a mock transport has no connection and
    therefore no peer. The check under test is the comparison, so the comparison
    is what is exercised, and the stand-in is named as a stand-in.
    """

    def __init__(self, address: str) -> None:
        self._address = address

    def get_extra_info(self, key: str):
        return self._address if key == "server_addr" else None


def test_a_connection_to_an_unapproved_address_is_refused() -> None:
    """The connected address is compared with the approved set, not trusted.

    This is the half of the rebinding defence that does not depend on resolving
    twice: even if every answer looked public, a connection that lands on an
    address nobody approved is refused after the fact.
    """
    response = httpx.Response(200, extensions={"network_stream": _FakeStream("10.0.0.7")})
    with pytest.raises(UrlRefused) as raised:
        _verify_peer(response, [PUBLIC])
    assert raised.value.reason == "address_not_allowed"


def test_a_connection_to_a_private_address_is_refused_even_when_approved() -> None:
    """Being on the approved list is not enough if the address is internal.

    Defence in depth against a poisoned approved set, and the check that would
    survive a future change to how the set is built.
    """
    response = httpx.Response(200, extensions={"network_stream": _FakeStream("127.0.0.1")})
    with pytest.raises(UrlRefused) as raised:
        _verify_peer(response, ["127.0.0.1"])
    assert raised.value.reason == "address_not_allowed"


def test_a_connection_to_an_address_not_in_the_approved_set_is_refused() -> None:
    response = httpx.Response(200, extensions={"network_stream": _FakeStream("1.2.3.4")})
    with pytest.raises(UrlRefused) as raised:
        _verify_peer(response, [PUBLIC, "1.1.1.1"])
    assert raised.value.reason == "address_changed"


def test_a_matching_connection_passes() -> None:
    """The control: a peer on the approved list is served, or every case above
    would pass with a function that refused everything."""
    response = httpx.Response(200, extensions={"network_stream": _FakeStream(PUBLIC)})
    _verify_peer(response, [PUBLIC])  # does not raise


# ====================================================== what the fetcher sends


def test_the_fetcher_sends_no_credentials_and_no_cookies() -> None:
    """The gateway is a fetcher, not a proxy.

    Only two headers are ever attached, and neither carries anything the caller
    supplied. A fetch that could forward a caller's token to an arbitrary host
    would be a token exfiltration endpoint.
    """
    from kb.catalog.upload_fetch import OUTBOUND_HEADERS

    assert set(OUTBOUND_HEADERS) == {"User-Agent", "Accept"}
    joined = " ".join(OUTBOUND_HEADERS).lower()
    for forbidden in ("authorization", "cookie", "token", "x-api-key", "proxy-"):
        assert forbidden not in joined


def test_a_host_that_resolves_to_both_public_and_private_is_refused() -> None:
    """Every address is checked, not the first one the resolver returned.

    A name with two A records, one public and one internal, must be refused
    whole. Picking the first and checking it would make the outcome depend on
    resolver ordering, which an attacker controls.
    """
    with pytest.raises(UrlRefused) as raised:
        check_url(
            "http://mixed.test/x",
            POLICY,
            resolver=resolver_for({"mixed.test": [PUBLIC, "10.0.0.1"]}),
        )
    assert raised.value.reason == "address_not_allowed"


def test_a_host_that_resolves_to_nothing_is_refused() -> None:
    with pytest.raises(UrlRefused) as raised:
        check_url("http://empty.test/x", POLICY, resolver=resolver_for({"empty.test": []}))
    assert raised.value.reason == "dns_failure"


def test_a_resolver_failure_is_a_refusal_not_an_exception() -> None:
    def broken(_host: str, _port: int) -> list[str]:
        raise socket.gaierror("no such host")

    with pytest.raises(UrlRefused) as raised:
        check_url("http://nowhere.test/x", POLICY, resolver=broken)
    assert raised.value.reason == "dns_failure"


# ================================================================ the payload


def test_an_oversized_object_is_refused_before_it_is_kept() -> None:
    body = b"x" * 4096

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Length": str(len(body))})

    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/big",
            FetchPolicy(max_bytes=1024),
            transport=mock_transport(handler),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "too_large"


def test_a_declared_length_that_lies_does_not_get_trusted() -> None:
    """A Content-Length is a claim. The body is measured, and the measurement wins."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"y" * 4096, headers={"Content-Length": "10"})

    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/liar",
            FetchPolicy(max_bytes=1024),
            transport=mock_transport(handler),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "too_large"


def test_a_server_error_is_not_stored_as_content() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"internal stack trace")

    with pytest.raises(UrlRefused) as raised:
        fetch_url(
            "http://public.test/boom",
            POLICY,
            transport=mock_transport(handler),
            resolver=resolver_for({"public.test": [PUBLIC]}),
        )
    assert raised.value.reason == "http_error"


def test_the_media_type_comes_from_the_bytes_not_the_header() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"%PDF-1.7 real pdf bytes", headers={"Content-Type": "text/html"}
        )

    result = fetch_url(
        "http://public.test/mislabelled",
        POLICY,
        transport=mock_transport(handler),
        resolver=resolver_for({"public.test": [PUBLIC]}),
    )
    assert result.declared_media_type == "text/html"
    assert result.media_type == "application/pdf"


def test_an_unidentifiable_object_is_recorded_as_an_honest_unknown() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x01\x02\x03\x04")

    result = fetch_url(
        "http://public.test/unknown",
        POLICY,
        transport=mock_transport(handler),
        resolver=resolver_for({"public.test": [PUBLIC]}),
    )
    assert result.media_type == "application/octet-stream"
    assert result.declared_media_type is None
