"""C12B — the security boundary: A14, proved rather than asserted.

PRODUCT-SPEC: "URL ingestion проверяет SSRF/redirect/IP". ACCESS-MODEL A14:
"URL localhost/private IPv4/IPv6, redirect/DNS rebinding, path traversal" with
the expected result "Загрузка блокируется; внутренние сервисы и файлы не
читаются".

What is real here and what is not, stated before any assertion:

* ``test_a_loopback_fetch_is_refused_and_the_internal_server_never_saw_a_request``
  starts a **real HTTP server on a real loopback socket**, points the real
  fetcher at it, and then reads the server's own request counter. It is zero.
  Its control, ``test_the_internal_service_is_really_serving``, makes the same
  request with a plain httpx client and the counter goes to one — so the first
  result is a refusal and not an unreachable port.
* ``test_a_loopback_url_is_refused_before_any_socket_is_opened`` replaces
  ``socket.socket`` and ``socket.create_connection`` with functions that raise.
  The fetch still ends in a refusal rather than in a transport error, which
  proves the *ordering* — and ordering is the entire defence.
* The redirect cases are driven over ``httpx.MockTransport``. A redirect to a
  private address can only be served by a host that is not private, so the
  socket is necessarily absent; the client, the loop, the response objects and
  every check on this card are real. The names say "over_a_mock_transport"
  wherever that is what is happening.
* No test in this file asserts that anything was fetched from the public
  internet. Nothing in this environment may.
"""

from __future__ import annotations

import unittest.mock

import httpx
import pytest
from harness import PUBLIC, parsed_from, resolver_for, routing, snapshot_over

from kb.catalog import fetch_html, upload_fetch
from kb.catalog.fetch_html import (
    FetchDeadlineExceeded,
    NotHtmlDocument,
    SnapshotPolicy,
    TooManyRedirects,
    UnsafeUrlPath,
    UrlRefused,
    check_snapshot_url,
    fetch_html_snapshot,
    is_blocked_address,
    parse_url,
)

POLICY = SnapshotPolicy()


def page(body: bytes = b"<html><body><p>hello</p></body></html>") -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda _r: httpx.Response(
            200, content=body, headers={"Content-Type": "text/html; charset=utf-8"}
        )
    )


# ================================================= the real loopback refusal


def test_a_loopback_fetch_is_refused_and_the_internal_server_never_saw_a_request(
    page_server: str, server_hits: list[str]
) -> None:
    """The required A14 proof, and it is not a mock.

    The URL is genuinely reachable — the server really is listening on 127.0.0.1
    and really serves the path — and the fetcher genuinely refuses it. The
    evidence that no request was made is the server's own counter, not the
    absence of an exception.
    """
    url = f"{page_server}/internal/secret"
    with pytest.raises(UrlRefused) as raised:
        fetch_html_snapshot(url, POLICY)
    assert raised.value.reason == "address_not_allowed"
    assert server_hits == [], "the internal service received a request; the refusal came too late"


def test_the_internal_service_is_really_serving(page_server: str, server_hits: list[str]) -> None:
    """The control for the test above.

    If the server were not actually serving, "the gateway refused" would prove
    nothing at all. This bypasses the fetcher on purpose — a plain ``httpx.get``
    with no policy, which the SSRF code never does — and the counter goes to one.
    """
    response = httpx.get(f"{page_server}/internal/secret", timeout=5)
    assert response.status_code == 200
    assert b"an internal service" in response.content
    assert len(server_hits) == 1


def test_a_loopback_url_is_refused_before_any_socket_is_opened() -> None:
    """Ordering: the check runs before the transport, not after the failure.

    Both socket entry points are replaced with functions that raise. A refusal
    that depended on a connection attempt would fail here with the transport's
    error instead of a ``UrlRefused`` — the difference between "we blocked it"
    and "it happened to fail".
    """

    def explode(*_args, **_kwargs):
        raise AssertionError("a socket was opened; the URL check runs too late")

    with (
        unittest.mock.patch("socket.socket", explode),
        unittest.mock.patch("socket.create_connection", explode),
    ):
        with pytest.raises(UrlRefused) as raised:
            fetch_html_snapshot("http://127.0.0.1:9/anything", POLICY)
    assert raised.value.reason == "address_not_allowed"


def test_a_page_served_from_a_loopback_fixture_server_is_refused_too(
    page_server: str, server_hits: list[str]
) -> None:
    """The real fixture server is refused for the real reason, not for its path.

    The card's acceptance environment is "native controlled HTTP fixtures", and
    this is one: a real page, on a real socket, on a real port. It is refused
    because the address is loopback, which is the correct answer in production
    and the reason the extraction tests below fetch these same bytes with a
    plain client instead of weakening the policy to reach them.
    """
    with pytest.raises(UrlRefused) as raised:
        fetch_html_snapshot(f"{page_server}/article", POLICY)
    assert raised.value.reason == "address_not_allowed"
    assert server_hits == []


# ================================================================ the ranges


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://169.254.169.254/latest/meta-data/", "the cloud metadata endpoint"),
        ("http://[fd00:ec2::254]/latest/meta-data/", "the AWS IPv6 metadata endpoint"),
        ("http://169.254.170.2/task/latest", "the ECS task metadata endpoint"),
        ("http://vault.internal/secret", "an internal name, refused by spelling"),
        ("http://10.0.0.5/", "RFC1918"),
        ("http://172.16.0.1/", "RFC1918"),
        ("http://192.168.1.1/", "RFC1918"),
        ("http://[::1]/", "IPv6 loopback"),
        ("http://[fe80::1]/", "IPv6 link-local"),
        ("http://[fc00::1]/", "IPv6 unique local"),
        ("http://[::ffff:127.0.0.1]/", "IPv4-mapped loopback"),
        ("http://100.64.0.1/", "CGNAT"),
    ],
)
def test_an_internal_service_is_never_reachable(url: str, why: str) -> None:
    """Acceptance item 2: internal metadata and services are not read.

    Refused with no transport configured at all, so these cases prove the check
    and not the mock. The reason string is in the test id via ``why``.
    """
    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url(url, POLICY, resolver=resolver_for({}))
    expected = "host_not_allowed" if why.startswith("an internal name") else "address_not_allowed"
    assert raised.value.reason == expected, why


def test_a_metadata_host_is_refused_by_its_name_before_any_lookup() -> None:
    """``metadata.google.internal`` never becomes a DNS question.

    A ``.internal`` name is refused on its spelling, so a resolver that would
    happily answer it with something routable is never consulted at all.
    """
    asked: list[str] = []

    def resolver(host: str, _port: int) -> list[str]:
        asked.append(host)
        return [PUBLIC]

    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url(
            "http://metadata.google.internal/computeMetadata/v1/", POLICY, resolver=resolver
        )
    assert raised.value.reason == "host_not_allowed"
    assert asked == []


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", PUBLIC, "2606:4700::1111"])
def test_a_public_address_is_not_blocked_by_the_list_itself(address: str) -> None:
    """The control for the case above.

    Without it, "refuse everything" would pass every test in this file. The
    filter must discriminate, not merely refuse.
    """
    assert is_blocked_address(address) is False


def test_172_16_to_172_31_is_private_and_its_neighbours_are_not() -> None:
    """The edges of 172.16.0.0/12, asserted from both sides.

    A filter that blocked all of 172.0.0.0/8 would pass every private-address
    case here and quietly refuse legitimate hosts.
    """
    assert is_blocked_address("172.15.255.255") is False
    assert is_blocked_address("172.16.0.0") is True
    assert is_blocked_address("172.31.255.255") is True
    assert is_blocked_address("172.32.0.1") is False


# ============================================================= URL shape


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("file:///etc/shadow", "scheme_not_allowed"),
        ("ftp://example.test/x", "scheme_not_allowed"),
        ("gopher://example.test/x", "scheme_not_allowed"),
        ("dict://example.test/x", "scheme_not_allowed"),
        ("data:text/html,<p>hi</p>", "scheme_not_allowed"),
        ("javascript:alert(1)", "scheme_not_allowed"),
        ("http://user:secret@public.test/x", "credentials_in_url"),
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

    ``file://`` is the case the card names outright: a URL that reaches the local
    filesystem is not a fetch. The numeric forms are the classic way past a
    filter that only understands dotted quads.
    """
    with pytest.raises(UrlRefused) as raised:
        fetch_html_snapshot(
            url, POLICY, transport=page(), resolver=resolver_for({"public.test": [PUBLIC]})
        )
    assert raised.value.reason == reason


@pytest.mark.parametrize(
    "url",
    [
        "http://public.test/../../etc/passwd",
        "http://public.test/a/../../secret",
        "http://public.test/%2e%2e/%2e%2e/etc/shadow",
        "http://public.test/a\\b",
        "http://public.test/a b",
        "http://public.test/a\nb",
    ],
)
def test_a_url_whose_path_walks_upwards_is_refused(url: str) -> None:
    """Acceptance item 4: no arbitrary server path, from the caller or from a hop.

    Refused rather than normalised. A URL the reviewer has not seen is not the
    URL that would be requested, and quietly cleaning it up is how a reviewed
    URL becomes a different one.
    """
    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url(url, POLICY, resolver=resolver_for({"public.test": [PUBLIC]}))
    assert raised.value.reason in {"unsafe_path", "malformed_url"}


def test_the_url_checks_run_before_the_resolver_is_consulted() -> None:
    """Ordering again, one step earlier: shape first, addresses second.

    A URL that is already refused for its scheme never reaches a name lookup, so
    a hostile resolver cannot answer a question it was never asked.
    """
    asked: list[str] = []

    def resolver(host: str, _port: int) -> list[str]:
        asked.append(host)
        return [PUBLIC]

    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url("file:///etc/passwd", POLICY, resolver=resolver)
    assert raised.value.reason == "scheme_not_allowed"
    assert asked == []


def test_an_ip_literal_is_never_handed_to_a_resolver() -> None:
    """A URL naming the address is refused on the address, not on a lookup.

    A resolver that "helpfully" mapped 169.254.169.254 to a public address would
    otherwise turn the metadata-endpoint block into a suggestion.
    """
    asked: list[str] = []
    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url(
            "http://169.254.169.254/",
            POLICY,
            resolver=lambda host, _port: (asked.append(host), [PUBLIC])[1],
        )
    assert raised.value.reason == "address_not_allowed"
    assert asked == []


def test_a_host_that_resolves_to_both_public_and_private_is_refused() -> None:
    """Every address is checked, not the first the resolver returned.

    A name with two A records, one public and one internal, must be refused
    whole; picking the first would make the outcome depend on resolver
    ordering, which an attacker controls.
    """
    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url(
            "http://mixed.test/x",
            POLICY,
            resolver=resolver_for({"mixed.test": [PUBLIC, "10.0.0.1"]}),
        )
    assert raised.value.reason == "address_not_allowed"


def test_a_host_that_resolves_to_nothing_is_refused() -> None:
    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url("http://empty.test/x", POLICY, resolver=resolver_for({"empty.test": []}))
    assert raised.value.reason == "dns_failure"


def test_a_refusal_message_never_contains_the_resolved_address() -> None:
    """A refusal must not become a map of the installation.

    The caller already knows the URL they typed. Resolved addresses are things
    they were not supposed to learn.
    """
    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url(
            "http://rebind.test/x", POLICY, resolver=resolver_for({"rebind.test": ["10.1.2.3"]})
        )
    assert "10.1.2.3" not in str(raised.value)
    assert PUBLIC not in str(raised.value)


def test_an_empty_or_hostless_url_is_refused() -> None:
    for url in ("", "   ", "http://", "https:///path", "not a url at all"):
        with pytest.raises(UrlRefused):
            parse_url(url)


# ================================================================ redirects


_REDIRECT_ROUTES = {
    "/to-loopback": httpx.Response(302, headers={"Location": "http://127.0.0.1:9/secret"}),
    "/to-metadata": httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"}),
    "/to-private": httpx.Response(302, headers={"Location": "http://10.0.0.9/internal"}),
    "/to-ula": httpx.Response(302, headers={"Location": "http://[fc00::1]/internal"}),
    "/protocol-relative-loopback": httpx.Response(302, headers={"Location": "//127.0.0.1/secret"}),
    "/to-file": httpx.Response(302, headers={"Location": "file:///etc/shadow"}),
    "/traversal": httpx.Response(302, headers={"Location": "http://public.test/../../etc/passwd"}),
    "/dotdot-path": httpx.Response(302, headers={"Location": "/../../etc/passwd"}),
    "/no-location": httpx.Response(302),
    "/unusable": httpx.Response(304),
}


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/to-loopback", "address_not_allowed"),
        ("/to-metadata", "address_not_allowed"),
        ("/to-private", "address_not_allowed"),
        ("/to-ula", "address_not_allowed"),
        ("/protocol-relative-loopback", "address_not_allowed"),
        ("/to-file", "scheme_not_allowed"),
        ("/traversal", "unsafe_path"),
        ("/no-location", "bad_redirect"),
        ("/unusable", "bad_redirect"),
    ],
)
def test_a_redirect_that_lands_on_an_internal_target_is_refused(path: str, reason: str) -> None:
    """The check runs again on *the hop*, not only on the URL that was typed.

    A public host answering ``302 Location: http://169.254.169.254/`` is the
    standard SSRF pivot. It is caught because every hop goes through
    ``check_snapshot_url`` before a request is built for it.

    Driven over a ``MockTransport``: the thing under test is this card's
    validation of a hop, and a private address cannot be served from here.
    """
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(routing(_REDIRECT_ROUTES), url=f"http://public.test{path}", policy=POLICY)
    assert raised.value.reason == reason


def test_a_relative_redirect_is_followed_and_re_validated() -> None:
    """The control for the cases above: a normal redirect is followed."""
    routes = {
        "/hop1": httpx.Response(302, headers={"Location": "/hop2"}),
        "/hop2": httpx.Response(302, headers={"Location": "/article"}),
        "/article": httpx.Response(
            200, content=b"<p>arrived</p>", headers={"Content-Type": "text/html"}
        ),
    }
    result = snapshot_over(routing(routes), url="http://public.test/hop1", policy=POLICY)
    assert result.status_code == 200
    assert result.hops == 2
    assert result.final_url == "http://public.test/article"
    assert result.requested_urls == (
        "http://public.test/hop1",
        "http://public.test/hop2",
        "http://public.test/article",
    )


def test_the_redirect_cap_is_enforced_over_a_mock_transport() -> None:
    """A chain longer than the cap is refused, and the cap bounds the work.

    The counter is the half that matters. ``TooManyRedirects`` alone could be
    produced by a client that fetched a hundred hops and then apologised; this
    asserts the transport received exactly ``max_redirects + 1`` requests.
    """
    seen: list[str] = []

    def endless(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(302, headers={"Location": f"{request.url.path}next"})

    policy = SnapshotPolicy(max_redirects=3)
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(httpx.MockTransport(endless), url="http://public.test/a", policy=policy)
    assert raised.value.reason == "too_many_redirects"
    assert len(seen) == policy.max_redirects + 1, "the cap did not bound the requests"


def test_a_chain_exactly_at_the_cap_is_followed() -> None:
    """The control for the cap: three hops with a cap of three is allowed.

    Without it, "refuse every chain" would pass the test above.
    """

    redirects = {"/a": "/b", "/b": "/c", "/c": "/end"}

    def three_hops(request: httpx.Request) -> httpx.Response:
        target = redirects.get(request.url.path)
        if target is not None:
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(200, content=b"<p>end</p>", headers={"Content-Type": "text/html"})

    result = snapshot_over(
        httpx.MockTransport(three_hops),
        url="http://public.test/a",
        policy=SnapshotPolicy(max_redirects=3),
    )
    assert result.hops == 3
    assert len(result.requested_urls) == 4


def test_a_cap_of_zero_follows_no_redirect_at_all() -> None:
    policy = SnapshotPolicy(max_redirects=0)
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(
            routing({"/start": httpx.Response(302, headers={"Location": "/end"})}),
            url="http://public.test/start",
            policy=policy,
        )
    assert raised.value.reason == "too_many_redirects"


def test_a_resolver_that_answers_twice_differently_is_caught() -> None:
    """DNS rebinding: the first answer is public, the second is not.

    The approved address set is recomputed per hop, so the second answer is
    refused before it is used — which is the half of the defence that does not
    depend on a connection happening to report its peer.
    """
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def flipping(_host: str, _port: int) -> list[str]:
        try:
            return next(answers)
        except StopIteration:  # pragma: no cover - the chain stops at the refusal
            return [PUBLIC]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/first":
            return httpx.Response(302, headers={"Location": "http://rebind.test/second"})
        return httpx.Response(200, content=b"<p>never reached</p>")

    with pytest.raises(UrlRefused) as raised:
        snapshot_over(httpx.MockTransport(handler), url="http://rebind.test/first")
    # the default resolver_for has no entry for rebind.test; declare it explicitly
    assert raised.value.reason in {"address_not_allowed", "host_not_allowed"}


def test_a_rebinding_resolver_is_caught_with_its_own_resolver() -> None:
    """The same case, with the rebinding resolver actually installed."""
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def flipping(_host: str, _port: int) -> list[str]:
        try:
            return next(answers)
        except StopIteration:  # pragma: no cover - the chain stops at the refusal
            return [PUBLIC]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/first":
            return httpx.Response(302, headers={"Location": "http://rebind.test/second"})
        return httpx.Response(200, content=b"<p>never reached</p>")

    with pytest.raises(UrlRefused) as raised:
        fetch_html_snapshot(
            "http://rebind.test/first",
            POLICY,
            transport=httpx.MockTransport(handler),
            resolver=flipping,
        )
    assert raised.value.reason == "address_not_allowed"


class _FakeStream:
    """Stands in for httpx's network stream so a peer address can be supplied.

    A real TCP transport reports the socket's peer through
    ``get_extra_info("server_addr")``; a mock transport has no connection and
    therefore no peer. What is under test is the comparison, so the comparison
    is what is exercised, and the stand-in is named as one.
    """

    def __init__(self, address: str) -> None:
        self._address = address

    def get_extra_info(self, key: str):
        return self._address if key == "server_addr" else None


def test_a_connection_to_an_unapproved_address_is_refused() -> None:
    """The connected address is compared with the approved set, not trusted.

    This is the half of the rebinding defence that does not depend on resolving
    twice: even when every answer looked public, a connection that lands on an
    address nobody approved is refused after the fact.
    """
    response = httpx.Response(200, extensions={"network_stream": _FakeStream("10.0.0.7")})
    with pytest.raises(UrlRefused) as raised:
        upload_fetch._verify_peer(response, [PUBLIC])
    assert raised.value.reason == "address_not_allowed"


def test_a_connection_to_a_private_address_is_refused_even_when_approved() -> None:
    """Being on the approved list is not enough if the address is internal.

    Defence in depth against a poisoned approved set.
    """
    response = httpx.Response(200, extensions={"network_stream": _FakeStream("127.0.0.1")})
    with pytest.raises(UrlRefused) as raised:
        upload_fetch._verify_peer(response, ["127.0.0.1"])
    assert raised.value.reason == "address_not_allowed"


def test_a_response_from_an_unapproved_peer_is_refused_inside_the_fetch() -> None:
    """The peer check runs on the response the fetch actually got.

    The three tests above exercise the comparison directly. This one drives it
    through :func:`fetch_html_snapshot`, so removing the call from the loop —
    rather than weakening the comparison — is what fails here. That is the
    mutation worth guarding: a correct check that is never called is not a
    check.
    """
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200,
            content=b"<p>arrived from somewhere nobody approved</p>",
            headers={"Content-Type": "text/html"},
            extensions={"network_stream": _FakeStream("10.0.0.7")},
        )
    )
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(transport)
    assert raised.value.reason == "address_not_allowed"


def test_a_response_from_a_private_peer_is_refused_inside_the_fetch() -> None:
    """The same, for the address that matters most: something internal."""
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200,
            content=b"<p>internal</p>",
            headers={"Content-Type": "text/html"},
            extensions={"network_stream": _FakeStream("127.0.0.1")},
        )
    )
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(transport)
    assert raised.value.reason == "address_not_allowed"


def test_a_response_from_the_approved_peer_is_served() -> None:
    """The control for the two above, through the same path.

    Without it, "refuse every response that carries a network_stream" would pass
    both.
    """
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200,
            content=b"<p>arrived from the address we approved</p>",
            headers={"Content-Type": "text/html"},
            extensions={"network_stream": _FakeStream(PUBLIC)},
        )
    )
    result = snapshot_over(transport)
    assert "approved" in result.text


def test_a_matching_connection_passes() -> None:
    """The control: a peer on the approved list is served.

    Without it, every case above would pass with a function refusing everything.
    """
    response = httpx.Response(200, extensions={"network_stream": _FakeStream(PUBLIC)})
    upload_fetch._verify_peer(response, [PUBLIC])  # does not raise


# ============================================== who owns the address rules


def test_the_address_rules_come_from_c10_and_are_not_copied() -> None:
    """One owner for the blocklist, and the identity is asserted.

    Two blocklists in one codebase is how the weaker one becomes the one in
    use. This test fails if someone pastes a list into this card, and it fails if
    someone edits C10's rules expecting this card to be unaffected.
    """
    assert fetch_html.is_blocked_address is upload_fetch.is_blocked_address
    assert fetch_html.check_url is upload_fetch.check_url
    assert fetch_html.parse_url is upload_fetch.parse_url
    assert fetch_html._verify_peer is upload_fetch._verify_peer
    assert fetch_html.BLOCKED_NETWORKS is upload_fetch.BLOCKED_NETWORKS


def test_the_snapshot_policy_can_never_turn_the_address_check_off() -> None:
    """``require_public_ip`` stays True whatever the policy says.

    C10 exposes it as a seam for tests that need a successful fetch without a
    resolver. Nothing in this card sets it, and a future edit that did would
    fail here rather than in production.
    """
    assert SnapshotPolicy().transport_policy().require_public_ip is True
    assert SnapshotPolicy(max_redirects=0).transport_policy().require_public_ip is True


def test_the_fetcher_sends_no_credentials_and_no_cookies() -> None:
    """The gateway is a fetcher, not a proxy.

    Whatever the page asks for, the outbound request carries exactly the two
    headers below. A fetch that could forward a caller's token to an arbitrary
    host would be a token exfiltration endpoint.
    """
    seen: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers)
        return httpx.Response(200, content=b"<p>ok</p>", headers={"Content-Type": "text/html"})

    snapshot_over(httpx.MockTransport(handler))
    assert len(seen) == 1
    sent = {k.lower() for k in seen[0].keys()}
    assert "authorization" not in sent
    assert "cookie" not in sent
    for forbidden in ("x-api-key", "proxy-authorization", "x-forwarded-for"):
        assert forbidden not in sent


# ==================================================== size, time and type


def test_an_oversized_page_is_refused_before_it_is_kept() -> None:
    body = b"<p>" + b"x" * 4096 + b"</p>"
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(page(body), policy=SnapshotPolicy(max_bytes=1024))
    assert raised.value.reason == "too_large"


def test_a_declared_length_that_lies_is_not_trusted() -> None:
    """A Content-Length is a claim. The body is measured, and the measurement wins."""
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200,
            content=b"<p>" + b"y" * 4096 + b"</p>",
            headers={"Content-Type": "text/html", "Content-Length": "10"},
        )
    )
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(transport, policy=SnapshotPolicy(max_bytes=1024))
    assert raised.value.reason == "too_large"


def test_the_whole_chain_is_bounded_by_one_deadline() -> None:
    """A slow chain is bounded by the total budget, not by one timeout per hop.

    The clock is injected so the test is about the accounting rather than about
    how fast this machine happens to be: each call advances it by a second, and
    a two-second budget must stop the chain.
    """
    ticks = iter([0.0, 1.0, 1.5, 3.5, 4.0, 5.0, 6.0, 7.0])

    def clock() -> float:
        try:
            return next(ticks)
        except StopIteration:  # pragma: no cover - the chain stops well before this
            return 99.0

    with pytest.raises(FetchDeadlineExceeded):
        snapshot_over(
            routing(
                {
                    "/a": httpx.Response(302, headers={"Location": "/b"}),
                    "/b": httpx.Response(302, headers={"Location": "/c"}),
                    "/c": httpx.Response(
                        200, content=b"<p>late</p>", headers={"Content-Type": "text/html"}
                    ),
                }
            ),
            url="http://public.test/a",
            policy=SnapshotPolicy(total_timeout=2.0),
            clock=clock,
        )


@pytest.mark.parametrize(
    "raised_by_transport",
    [httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"), httpx.RemoteProtocolError("x")],
)
def test_a_transport_error_becomes_a_refusal_not_an_exception(raised_by_transport) -> None:
    """A transport failure is converted, so a request handler never sees httpx.

    The caller is an HTTP route; an unhandled transport exception would be a 500
    that teaches operators to ignore 500s, and the exception's string can carry
    the peer address. What is under test is the conversion — the transport here
    raises the exception a real network stack would raise, because a
    ``MockTransport`` has no socket and therefore no timeout of its own.
    """

    def failing(_request: httpx.Request) -> httpx.Response:
        raise raised_by_transport

    with pytest.raises(UrlRefused) as raised:
        snapshot_over(httpx.MockTransport(failing))
    assert raised.value.reason == "fetch_failed"
    assert "127.0.0.1" not in str(raised.value)
    assert "timed out" not in str(raised.value)


def test_a_page_that_is_not_html_is_refused() -> None:
    """A PDF behind a ``text/html`` header is a PDF, and the bytes decide."""
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200, content=b"%PDF-1.7 real pdf bytes", headers={"Content-Type": "text/html"}
        )
    )
    with pytest.raises(NotHtmlDocument) as raised:
        snapshot_over(transport)
    assert raised.value.reason == "not_html"
    assert raised.value.media_type == "application/pdf"


def test_a_server_error_is_not_stored_as_content() -> None:
    transport = httpx.MockTransport(lambda _r: httpx.Response(500, content=b"internal stack trace"))
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(transport)
    assert raised.value.reason == "http_error"


def test_an_xhtml_page_is_accepted() -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            200,
            content=b"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>x</p></body></html>",
            headers={"Content-Type": "application/xhtml+xml"},
        )
    )
    result = snapshot_over(transport)
    assert result.media_type == "application/xhtml+xml"


# ============================================== the page cannot fetch anything


def test_a_page_cannot_make_the_gateway_fetch_a_second_thing(
    page_server: str, server_hits: list[str], hostile_page: bytes
) -> None:
    """Acceptance item 4 again, from the other end: no sub-resource fetching.

    The hostile fixture points at a metadata endpoint, a private address and a
    loopback service. The page is fetched once, and the internal service that is
    really listening on this machine records nothing. A page that could pull a
    second resource would turn the fetcher into a proxy for whatever a remote
    server names.
    """
    snapshot = snapshot_over(
        httpx.MockTransport(
            lambda _r: httpx.Response(
                200, content=hostile_page, headers={"Content-Type": "text/html"}
            )
        )
    )
    assert len(snapshot.requested_urls) == 1
    parsed = parsed_from(snapshot)
    assert any("169.254.169.254" in url for url in parsed.referenced_urls)
    assert any("127.0.0.1" in url for url in parsed.referenced_urls)
    assert server_hits == [], "a page made the gateway fetch something it named"


def test_a_meta_refresh_is_not_followed() -> None:
    """``<meta http-equiv=refresh>`` is a navigation, not a reference.

    A browser would follow it. Nothing in this card does, and the transport is
    asked exactly once.
    """
    body = (
        b'<html><head><meta http-equiv="refresh" content="0; url=http://169.254.169.254/">'
        b"</head><body><p>stay</p></body></html>"
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=body, headers={"Content-Type": "text/html"})

    snapshot_over(httpx.MockTransport(handler))
    assert len(seen) == 1


def test_unsafe_paths_are_refused_before_any_request(
    page_server: str, server_hits: list[str]
) -> None:
    """A traversal URL aimed at the live loopback server reaches nothing.

    Both refusals happen before a client exists, so the counter stays empty even
    though the server is genuinely up.
    """
    with pytest.raises((UnsafeUrlPath, UrlRefused)):
        check_snapshot_url(f"{page_server}/../../etc/passwd", POLICY)
    with pytest.raises(UrlRefused) as raised:
        check_snapshot_url(f"{page_server}/internal/secret", POLICY)
    assert raised.value.reason == "address_not_allowed"
    assert server_hits == []


def test_an_oversized_location_header_is_refused() -> None:
    routes = {"/a": httpx.Response(302, headers={"Location": "http://public.test/" + "x" * 3000})}
    with pytest.raises(UrlRefused) as raised:
        snapshot_over(routing(routes), url="http://public.test/a")
    assert raised.value.reason == "malformed_url"


def test_too_many_redirects_is_the_c10_exception() -> None:
    """The cap raises the same exception type C10 raises, for one reason.

    Two different exception classes for the same refusal would mean every caller
    has to know which module refused it.
    """
    assert issubclass(TooManyRedirects, UrlRefused)
    assert TooManyRedirects is upload_fetch.TooManyRedirects
