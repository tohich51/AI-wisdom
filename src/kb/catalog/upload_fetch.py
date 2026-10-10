"""C10 — fetching a source from a URL without turning the gateway into a proxy.

PRODUCT-SPEC: "URL ingestion проверяет SSRF/redirect/IP". ARCHITECTURE.md §10 is
more specific: "URL проходит защиту от SSRF, включая повторную проверку каждого
redirect и разрешённого IP при соединении". ACCESS-MODEL A14 is the scenario:
localhost, private IPv4/IPv6, redirect and DNS rebinding, path traversal.

So this module does five things and refuses loudly on all of them:

1. **Scheme.** Only ``http`` and ``https``. ``file:``, ``ftp:``, ``gopher:``,
   ``dict:`` and friends are refused before anything is parsed further.
2. **Host shape.** No userinfo (``http://user:pw@host``), no empty host, and no
   name that is localhost-ish by spelling alone.
3. **IP.** Every address the host resolves to is checked against the blocked
   ranges before the request is built. Loopback, private, link-local (which is
   where the cloud metadata endpoint lives), CGNAT, the IPv4/IPv6
   documentation ranges, multicast and reserved are all refused.
4. **Every redirect hop.** ``follow_redirects`` is OFF. Each ``Location`` is
   re-parsed and re-checked from step 1, the hop count is capped, and a hop to
   a non-http(s) scheme is refused rather than followed.
5. **The address actually connected to.** After the response, the peer address
   is compared with the addresses that were approved. This is what catches DNS
   rebinding, where the name passed the check and the connection landed
   somewhere else.

The check is a *precondition of the connection*, not a post-hoc filter. The
``test_a_loopback_url_is_refused_before_any_socket_is_opened`` case proves the
ordering rather than asserting it: it replaces ``socket.socket`` and
``socket.create_connection`` with functions that raise, and the fetch still ends
in a refusal rather than in a crash from the transport.

Nothing here is mocked at the boundary. The unit tests for redirects and rebinding
drive a real ``httpx.Client`` over a real ``MockTransport``, because the behaviour
under test is *this* module's control flow over httpx's real response objects;
the thing that is faked is only the network, and that is stated in the test names.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

# Only these two. Everything else is refused before it is parsed further, which
# is the difference between "not supported" and "not even looked at".
ALLOWED_SCHEMES = ("http", "https")

# Host *names* that are internal by spelling. Checked before resolution so a
# `.internal` name is refused even on an installation whose resolver would
# happily hand back a public address for it.
_BLOCKED_HOST_SUFFIXES = (
    "localhost",
    ".localhost",
    ".local",
    ".internal",
    ".intranet",
    ".home.arpa",
)

# Cloud instance metadata lives on link-local, and the two well-known addresses
# are named here so the intent survives a refactor: 169.254.169.254 (AWS/Azure/
# GCP/OpenStack) and fd00:ec2::254 (AWS IPv6). Both are inside the blocked
# ranges below; they are called out because they are the reason 169.254.0.0/16
# is on the list at all, and a reader should not have to re-derive that.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # CGNAT — RFC 6598, a real network in someone's datacentre
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, and the metadata endpoint
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # TEST-NET-1
        "192.88.99.0/24",  # 6to4 relay anycast
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # TEST-NET-2
        "203.0.113.0/24",  # TEST-NET-3
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, includes 255.255.255.255
        "::/128",  # unspecified
        "::1/128",  # IPv6 loopback
        "64:ff9b::/96",  # NAT64 — maps onto v4, including v4 loopback
        "100::/64",  # discard-only
        "2001:db8::/32",  # documentation
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local, and the IPv6 metadata endpoint
        "ff00::/8",  # multicast
    )
)

# The only headers this fetcher will ever send. No Authorization, no Cookie, no
# forward of anything the caller sent: the gateway is a fetcher, not a proxy,
# and a fetch that could carry a caller's token to an arbitrary host is a token
# exfiltration endpoint.
OUTBOUND_HEADERS = {
    "User-Agent": "knowledge-hub-source-fetch/1",
    "Accept": "*/*",
}

Resolver = Callable[[str, int], Sequence[str]]


class UrlRefused(RuntimeError):
    """A URL was refused before or during the fetch.

    ``reason`` is a short machine-readable code. It never contains the resolved
    address, the response body or anything the caller supplied beyond the URL
    they already know.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class TooManyRedirects(UrlRefused):
    def __init__(self, hops: int) -> None:
        super().__init__(
            "too_many_redirects", f"refusing a URL that redirects more than {hops} times"
        )
        self.hops = hops


class FetchTooLarge(UrlRefused):
    def __init__(self, limit: int) -> None:
        super().__init__("too_large", f"the remote object is larger than {limit} bytes")
        self.limit = limit


@dataclass(frozen=True)
class FetchPolicy:
    """Everything adjustable about a fetch, with the v1 defaults filled in.

    A policy is data, not a global, so a test can shrink the limits without
    monkeypatching the module and a future installation can change them without
    a code edit. None of these values comes from the request.
    """

    max_redirects: int = 5
    max_bytes: int = 256 * 1024 * 1024
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    # When False the IP check is skipped. It exists for exactly one reason: a
    # test that must exercise a *successful* fetch over a mock transport without
    # a resolver. Nothing in the product sets it to False, and the resolver is
    # injected rather than skipped by default, so the default is always the safe
    # one.
    require_public_ip: bool = True


@dataclass(frozen=True)
class FetchedObject:
    content: bytes
    # What the server *said*. It is a claim, not a fact, and it is only recorded
    # after the sniffed type below had its say.
    declared_media_type: str | None
    # What the bytes actually look like, or 'application/octet-stream' when
    # nothing could be determined. A header is never trusted over the bytes.
    media_type: str
    final_url: str
    status_code: int
    hops: int
    approved_addresses: tuple[str, ...] = field(default=())


# ------------------------------------------------------------------ checking


def is_blocked_address(address: str) -> bool:
    """True when an IP literal must never be connected to.

    Both tests are applied and both must pass for the address to be allowed:
    the explicit list above, and ``is_global``. The second is redundant today
    and is kept anyway, because ``is_global`` covers ranges added to the IANA
    registries that this list has not been updated for, and a redundant refusal
    is a better failure than a new kind of access.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if any(ip in network for network in BLOCKED_NETWORKS):
        return True
    return not ip.is_global


def default_resolver(host: str, port: int) -> Sequence[str]:
    """Resolve a host to every address it answers with.

    All of them, not the first: a name with two A records where one is public
    and one is not must be refused, and picking one and checking it would make
    the outcome depend on which one the resolver happened to order first.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def parse_url(url: str) -> tuple[str, str, int, str]:
    """``(scheme, host, port, path)`` after the shape checks.

    Every rejection here happens before a socket exists, a resolver is called or
    a header is built. That ordering is the whole point.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlRefused("malformed_url", "no URL was supplied")
    if len(url) > 2048:
        raise UrlRefused("malformed_url", "the URL is too long")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRefused(
            "scheme_not_allowed",
            f"only {' and '.join(ALLOWED_SCHEMES)} URLs can be fetched, not {scheme or 'a bare'}:",
        )
    if parts.username or parts.password:
        # Credentials in a URL would be sent to whatever host the URL names, and
        # would survive a redirect into a log line. Neither is acceptable.
        raise UrlRefused("credentials_in_url", "a URL may not carry a username or a password")
    host = (parts.hostname or "").lower()
    if not host:
        raise UrlRefused("malformed_url", "the URL names no host")
    # Alternate numeric forms of an address — 2130706433, 0x7f000001, 0177.0.0.1
    # — are the classic way past a filter that only understands dotted quads.
    # ``ipaddress`` rejects them and a resolver may or may not expand them, so
    # they are refused here instead of being handed on and hoping.
    if host.isdigit() or host.startswith(("0x", "0o")):
        raise UrlRefused("address_not_allowed", "the URL names an address in a numeric form")
    if any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in host.split(".")
        if part
    ):
        raise UrlRefused("address_not_allowed", "the URL names an address in a numeric form")
    lowered = host.rstrip(".")
    if lowered in _BLOCKED_HOST_SUFFIXES or any(
        lowered.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES if suffix.startswith(".")
    ):
        raise UrlRefused("host_not_allowed", f"{host} names an internal host")
    try:
        port = parts.port
    except ValueError as exc:
        raise UrlRefused("malformed_url", "the URL has an invalid port") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port, parts.path or "/"


def _addresses_for(
    host: str, port: int, policy: FetchPolicy, resolver: Resolver | None
) -> Sequence[str]:
    """Every address this URL could connect to.

    An IP literal is its own answer and is never handed to a resolver at all.
    That matters: a URL of ``http://169.254.169.254/`` must be refused because of
    the address written *in it*, not because of whatever a resolver happens to
    answer for it. A resolver that "helpfully" mapped a link-local literal to a
    public address would otherwise turn the check into a suggestion.
    """
    if not policy.require_public_ip:
        return ()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [host]
    resolve = resolver or default_resolver
    return list(resolve(host, port))


def check_url(
    url: str,
    policy: FetchPolicy,
    *,
    resolver: Resolver | None = None,
) -> tuple[str, str, int, str]:
    """Validate a URL and every address it resolves to. Returns the parse.

    Called once before the first request and again for every ``Location``.
    """
    scheme, host, port, path = parse_url(url)
    if not policy.require_public_ip:
        return scheme, host, port, path
    try:
        addresses = _addresses_for(host, port, policy, resolver)
    except OSError as exc:
        raise UrlRefused("dns_failure", f"the host {host} could not be resolved") from exc
    if not addresses:
        raise UrlRefused("dns_failure", f"the host {host} resolved to no address")
    for address in addresses:
        if is_blocked_address(address):
            # The reason names the range, not the literal address. The caller
            # supplied the URL; they do not need the address, and a log line
            # full of resolved addresses is a map of the installation.
            raise UrlRefused(
                "address_not_allowed", f"{host} resolves to an address that is not public"
            )
    return scheme, host, port, path


def _peer_address(response: httpx.Response) -> str | None:
    """The address the client actually connected to, if it will tell us.

    httpx exposes this on the network stream for TCP transports. It is absent
    for transports that have no connection (a mock), which is precisely why
    this is a strengthening check and not the primary one.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    getter = getattr(stream, "get_extra_info", None)
    if getter is None:  # pragma: no cover - every httpx transport stream has it
        return None
    addr = getter("server_addr")
    if addr is None:
        return None
    if isinstance(addr, tuple):
        return str(addr[0])
    return str(addr)


def _verify_peer(response: httpx.Response, approved: Sequence[str]) -> None:
    """Refuse a response that came from an address nobody approved.

    This is the DNS-rebinding half of A14. The name was checked before the
    request; this checks that the connection agreed. The two together mean a
    hostile resolver cannot win by answering twice.
    """
    if not approved:
        return
    peer = _peer_address(response)
    if peer is None:
        return
    if is_blocked_address(peer):
        raise UrlRefused(
            "address_not_allowed", "the connection was made to an address that is not public"
        )
    if peer not in approved:
        raise UrlRefused(
            "address_changed",
            "the address the connection reached is not one of the approved addresses for this host",
        )


# ------------------------------------------------------------------ fetching


def fetch_url(
    url: str,
    policy: FetchPolicy,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> FetchedObject:
    """Fetch ``url``, applying the checks above before and during the transfer.

    Redirects are followed by hand rather than by httpx for one reason: httpx
    would follow them, and the check has to run on *each* hop. ``follow_redirects``
    is left off the client so that no hop can happen without passing through
    ``check_url`` first.
    """
    check_url(url, policy, resolver=resolver)

    limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
    timeout = httpx.Timeout(policy.read_timeout, connect=policy.connect_timeout)
    with httpx.Client(
        follow_redirects=False,  # deliberate: every hop goes through check_url
        transport=transport,
        limits=limits,
        timeout=timeout,
        headers=OUTBOUND_HEADERS,
    ) as client:
        current = url
        approved: Sequence[str] = ()
        for hop in range(policy.max_redirects + 1):
            _scheme, host, port, _path = check_url(current, policy, resolver=resolver)
            approved = _approved(host, port, policy, resolver)
            try:
                response = client.get(current)
            except httpx.InvalidURL as exc:
                # A URL httpx itself will not build. Refused, not surfaced as a
                # 500: the caller sent it, and a 500 teaches operators to
                # ignore 500s.
                raise UrlRefused("malformed_url", "the URL could not be parsed") from exc
            except httpx.HTTPError as exc:
                # A transport error may carry the peer address in its string.
                # Only the exception class is propagated.
                raise UrlRefused("fetch_failed", f"the fetch failed: {type(exc).__name__}") from exc
            _verify_peer(response, approved)

            if response.is_redirect:
                location = response.headers.get("location", "")
                response.close()
                if hop >= policy.max_redirects:
                    raise TooManyRedirects(policy.max_redirects)
                if not location:
                    raise UrlRefused("bad_redirect", "the redirect carried no destination")
                current = _next_url(current, location)
                # the loop body re-runs check_url on the new URL: that is the
                # per-hop re-validation, and it is why follow_redirects is off
                continue

            if response.status_code >= 400:
                raise UrlRefused("http_error", f"the remote server answered {response.status_code}")
            body = _bounded_body(response, policy.max_bytes)
            return FetchedObject(
                content=body,
                declared_media_type=response.headers.get("content-type"),
                media_type=sniff_media_type(body, response.headers.get("content-type")),
                final_url=current,
                status_code=response.status_code,
                hops=hop,
                approved_addresses=tuple(approved),
            )
    raise TooManyRedirects(policy.max_redirects)  # pragma: no cover - loop always returns or raises


def _approved(
    host: str, port: int, policy: FetchPolicy, resolver: Resolver | None
) -> Sequence[str]:
    try:
        return list(_addresses_for(host, port, policy, resolver))
    except OSError:  # pragma: no cover - check_url already resolved this host
        return ()


def _next_url(current: str, location: str) -> str:
    """Resolve a ``Location`` against the URL it came from.

    A relative ``Location`` is normal HTTP and is joined here. An absolute one
    replaces the URL entirely — which is exactly the case a hostile server uses
    to point the fetch at ``http://169.254.169.254/``, and which is why the
    joined result goes back through ``check_url`` on the next iteration.
    """
    joined = urljoin(current, location.strip())
    if len(joined) > 2048:
        raise UrlRefused("malformed_url", "the redirect target is too long")
    return joined


def _bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Read the body, refusing it if it is over the limit.

    The ``Content-Length`` is checked first so an oversized download is refused
    before it is transferred, and the real length is checked again while reading
    because a header is a claim.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise FetchTooLarge(max_bytes)
        except ValueError:
            pass  # an unparseable Content-Length is ignored, not trusted
    body = response.content
    if len(body) > max_bytes:
        raise FetchTooLarge(max_bytes)
    return body


# ---------------------------------------------------------------- sniffing

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"PK\x03\x04", "application/zip"),  # also the container of docx/epub
)


def sniff_media_type(body: bytes, declared: str | None) -> str:
    """What the bytes are, with the header used only as a fallback.

    Deliberate behaviours:

    * a PDF or PNG magic number beats a server that says ``text/html``. Bytes do
      not lie to get-likeness and headers do;
    * ``text/html`` is an accepted answer, not an error: an HTML page is a
      legitimate source in this product and ``html_snapshot`` is a first-class
      locator. The risk that a fetch returned a login wall is real but it is
      recorded honestly as what the server sent rather than as what the owner
      meant to upload, and the caller sees the media type;
    * when nothing matches and nothing was declared, the answer is
      ``application/octet-stream`` — an honest unknown, not a guess.
    """
    for magic, media_type in _SIGNATURES:
        if body.startswith(magic):
            return media_type
    if declared:
        base = declared.split(";", 1)[0].strip().lower()
        if base:
            return base
    return "application/octet-stream"
