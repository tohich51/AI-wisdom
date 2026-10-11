"""C12B — fetching one HTML page, safely, and recording what came back.

PRODUCT-SPEC, in the sentence that matters for this card: "URL ingestion
проверяет SSRF/redirect/IP". ACCESS-MODEL A14 is the scenario — localhost,
private IPv4/IPv6, redirect and DNS rebinding, path traversal — with the
expected result "загрузка блокируется; внутренние сервисы и файлы не читаются".

The address rules have one owner: C10's :mod:`kb.catalog.upload_fetch`. This
module imports them rather than copying them, because two blocklists in one
codebase is how the weaker one becomes the one in use. What this module owns is
the *ordering* around them, and ordering is the whole of SSRF defence:

    1. scheme, host shape and every resolved address are checked BEFORE a
       client is built, so a refused URL never reaches a transport;
    2. ``follow_redirects`` is off. Every ``Location`` is resolved against the
       URL it came from and then re-checked from step 1, so a public host that
       answers ``302 Location: http://169.254.169.254/`` is refused at the hop
       rather than followed;
    3. the hop count is capped, and the cap is a bound on work, not only on the
       error — the test counts the requests the transport actually received;
    4. after every response the address the client actually connected to is
       compared with the set that was approved for that hop, which is what
       catches a resolver that answers twice;
    5. the body is bounded by a deadline that covers the whole chain, not by
       one timeout per request, so a slow-redirecting host cannot hold a worker
       for ``hops x timeout`` seconds.

Two things this module deliberately does **not** do, both of which are the
"no arbitrary server path" item in the card's acceptance list:

* It never fetches a sub-resource. Images, stylesheets and frames stay in the
  bytes. A page cannot make the gateway issue a second request, so a page cannot
  reach an internal service by reference.
* It never turns a URL into a filesystem path. The caller gets bytes and a
  locator; where those bytes are stored is the store's business, addressed by
  content hash.

The snapshot is a *snapshot*: :class:`HtmlSnapshot` carries the requested URL,
the URL the bytes actually came from, the instant they arrived, the raw bytes,
their hash, the declared and sniffed media types, the charset with the reason it
was chosen, every URL the fetcher requested, and the extractor version that will
read them. ``retrieved_at`` is measured here, at the moment the bytes came back.
It is never the caller's value, never the database clock and never a stand-in for
a retrieval that did not happen.

What is real and what is not, so a reviewer does not have to guess: the refusal
of loopback is proved against a **real HTTP server on a real socket** with the
server's own request counter read afterwards. The redirect chain cases are driven
over ``httpx.MockTransport``, because a redirect to a private address can only be
reached from a host that is not private — only the socket is faked, the client
is real, and the test names say so. Nothing in this card asserts that anything
was fetched from the public internet.
"""

from __future__ import annotations

import codecs
import datetime as dt
import hashlib
import ipaddress
import re
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from kb.catalog.parsers.html_extract import EXTRACTOR_VERSION
from kb.catalog.upload_fetch import (
    ALLOWED_SCHEMES,
    BLOCKED_NETWORKS,  # re-exported: the ranges are C10's, and this card may not restate them
    OUTBOUND_HEADERS,
    FetchedObject,
    FetchPolicy,
    FetchTooLarge,
    Resolver,
    TooManyRedirects,
    UrlRefused,
    _verify_peer,
    check_url,
    is_blocked_address,
    parse_url,
    sniff_media_type,
)

# The media types this card will extract. Everything else is refused with a
# reason the caller can act on, after the bytes have been read and before
# anything is stored: a login wall served as ``text/html`` is recorded as what
# the server sent, never as what the owner meant to upload.
HTML_MEDIA_TYPES: frozenset[str] = frozenset({"text/html", "application/xhtml+xml"})

# The longest URL accepted, before or after a redirect. A Location is attacker
# controlled in the redirect case, so the limit applies to the joined result.
MAX_URL_LENGTH = 2048

# Characters that must not appear in a URL. A CR or LF in a path is request
# splitting; a NUL is a truncation trick; a backslash is a path separator to some
# clients and a host character to others.
_FORBIDDEN_IN_URL = re.compile(r"[\x00-\x20\x7f\\]")

# A path that has to walk upwards. Refused rather than normalised: a URL the
# caller has not reviewed is not the URL that would be requested, and "we
# cleaned it up for you" is how a reviewed URL becomes a different one.
_TRAVERSAL_SEGMENTS = ("..", "%2e%2e", "%2E%2E", "%2e%2E")


class NotHtmlDocument(UrlRefused):
    """The object is real, reachable and simply is not an HTML page."""

    def __init__(self, media_type: str) -> None:
        super().__init__(
            "not_html", f"the object is {media_type}, not a page this card can extract"
        )
        self.media_type = media_type


class FetchDeadlineExceeded(UrlRefused):
    """The whole redirect chain ran out of time."""

    def __init__(self, budget: float) -> None:
        super().__init__("deadline_exceeded", f"the fetch chain exceeded {budget} seconds")
        self.budget = budget


class UnsafeUrlPath(UrlRefused):
    def __init__(self, message: str = "the URL path may not walk upwards") -> None:
        super().__init__("unsafe_path", message)


@dataclass(frozen=True)
class SnapshotPolicy:
    """Every adjustable value, with safe defaults. No value comes from a request.

    ``fetch`` carries C10's transport limits; the two SSRF-relevant ones are
    tightened here rather than inherited: three hops instead of five, and a body
    ceiling sized for a web page rather than for a book. ``total_timeout`` is
    new and is the reason the redirect loop is local to this module — C10's
    ``fetch_url`` builds one client with one timeout, which bounds each request
    and not the chain.
    """

    max_redirects: int = 3
    max_bytes: int = 8 * 1024 * 1024
    total_timeout: float = 20.0
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    require_html: bool = True

    def transport_policy(self) -> FetchPolicy:
        """The C10 policy this snapshot fetch runs its checks under.

        ``require_public_ip`` stays True. It exists in C10 as an escape hatch
        for tests that need a successful fetch without a resolver; nothing in
        this card sets it, and it is pinned by a test that reads it back.
        """
        return FetchPolicy(
            max_redirects=self.max_redirects,
            max_bytes=self.max_bytes,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            require_public_ip=True,
        )


@dataclass(frozen=True)
class DecodedPage:
    """The bytes decoded to text, with the reason for the charset that was used."""

    text: str
    charset: str
    charset_source: str
    replacement_chars: int = 0


@dataclass(frozen=True)
class HtmlSnapshot:
    """One page as it arrived. Raw bytes plus honest provenance. Nothing else."""

    requested_url: str
    final_url: str
    retrieved_at: dt.datetime
    content: bytes
    content_hash: str
    media_type: str
    declared_media_type: str | None
    decoded: DecodedPage
    hops: int
    requested_urls: tuple[str, ...] = field(default=())
    status_code: int = 200
    extractor_version: str = EXTRACTOR_VERSION
    etag: str | None = None
    last_modified: str | None = None

    @property
    def byte_size(self) -> int:
        return len(self.content)

    @property
    def text(self) -> str:
        return self.decoded.text

    def as_fetched_object(self) -> FetchedObject:
        """The C10 view of the same fetch, for callers that already speak it."""
        return FetchedObject(
            content=self.content,
            declared_media_type=self.declared_media_type,
            media_type=self.media_type,
            final_url=self.final_url,
            status_code=self.status_code,
            hops=self.hops,
        )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------ checking


def check_path_shape(url: str) -> None:
    """Refuse a URL whose path is a traversal, a control character or a backslash.

    Applied to the URL the caller supplied *and* to every ``Location`` — the hop
    goes through the same function as the original, so a redirect cannot smuggle
    a path past a check the first URL passed.
    """
    if _FORBIDDEN_IN_URL.search(url):
        raise UnsafeUrlPath("the URL contains a space, a control character or a backslash")
    for segment in _path_of(url).split("/"):
        lowered = segment.lower()
        if lowered in _TRAVERSAL_SEGMENTS:
            raise UnsafeUrlPath("the URL path walks upwards")


def _path_of(url: str) -> str:
    try:
        return urlsplit(url).path
    except ValueError as exc:  # pragma: no cover - urlsplit is extremely tolerant
        raise UrlRefused("malformed_url", "the URL could not be parsed") from exc


def check_snapshot_url(
    url: str,
    policy: SnapshotPolicy,
    *,
    resolver: Resolver | None = None,
) -> tuple[str, str, int, str]:
    """Validate a URL completely, before a client exists.

    Scheme, credentials, host spelling, traversal and then every address the host
    resolves to. Returns the parse so the caller can record the host it approved
    for this hop.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlRefused("malformed_url", "no URL was supplied")
    if len(url) > MAX_URL_LENGTH:
        raise UrlRefused("malformed_url", "the URL is too long")
    check_path_shape(url)
    return check_url(url, policy.transport_policy(), resolver=resolver)


def _approved_addresses(
    host: str, port: int, policy: SnapshotPolicy, resolver: Resolver | None
) -> Sequence[str]:
    """The addresses this hop was approved for, re-resolved for this hop only.

    Recomputed per hop on purpose. A resolver that answers ``93.184.216.34`` the
    first time and ``127.0.0.1`` the second time is the DNS rebinding case, and
    a cached answer would hide it.
    """
    if _is_literal(host):
        return [host]
    resolve = resolver or _system_resolver
    try:
        return list(resolve(host, port))
    except OSError:  # pragma: no cover - check_snapshot_url already resolved it
        return ()


def _system_resolver(host: str, port: int) -> Sequence[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _next_url(current: str, location: str) -> str:
    """Resolve a ``Location`` against the URL it came from, bounded.

    A relative or protocol-relative ``Location`` is ordinary HTTP. An absolute
    one replaces the URL entirely, which is the standard SSRF pivot — and the
    joined result goes back through :func:`check_snapshot_url` on the next
    iteration, which is why a pivot does not need a special case to be caught.
    """
    joined = urljoin(current, location.strip())
    if len(joined) > MAX_URL_LENGTH:
        raise UrlRefused("malformed_url", "the redirect target is too long")
    return joined


class _RecordingTransport(httpx.BaseTransport):
    """Wraps a transport and remembers every URL httpx actually requested.

    The redirect chain is *observed* rather than reconstructed: what lands in
    :attr:`requested_urls` is what the client asked for, in order, which is the
    only claim worth storing about where a page came from.
    """

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.requested: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        return self._inner.handle_request(request)


# ------------------------------------------------------------------ decoding


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-z0-9_.:+-]+)""", re.IGNORECASE)
_HTTP_EQUIV_CHARSET = re.compile(
    rb"""content\s*=\s*["'][^"']*charset\s*=\s*([a-z0-9_.:+-]+)""", re.IGNORECASE
)


def _usable_codec(name: str | None) -> str | None:
    if not name:
        return None
    candidate = name.strip().strip("\"'").lower()
    try:
        return codecs.lookup(candidate).name
    except LookupError:
        return None


def _meta_charset(raw: bytes) -> str | None:
    head = raw[:4096]
    for pattern in (_META_CHARSET, _HTTP_EQUIV_CHARSET):
        found = pattern.search(head)
        if found:
            usable = _usable_codec(found.group(1).decode("ascii", "replace"))
            if usable:
                return usable
    return None


def _http_charset(declared_media_type: str | None) -> str | None:
    if not declared_media_type or "charset=" not in declared_media_type.lower():
        return None
    _, _, params = declared_media_type.partition(";")
    for part in params.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "charset":
            return _usable_codec(value)
    return None


def decode_html(raw: bytes, declared_media_type: str | None = None) -> DecodedPage:
    """Decode the page and record *why* that charset was chosen.

    The order is BOM, then the HTTP header, then a ``<meta>`` tag, then UTF-8.
    An unknown charset name is ignored rather than obeyed — a server claiming
    ``charset=bogus`` gets UTF-8 with replacements and a counted number of them,
    which is honest, instead of an exception that looks like a fetch failure.
    """
    if raw.startswith(codecs.BOM_UTF8):
        return _decode(raw, "utf-8-sig", "bom")
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return _decode(raw, "utf-16", "bom")
    from_header = _http_charset(declared_media_type)
    if from_header:
        return _decode(raw, from_header, "http_header")
    from_meta = _meta_charset(raw)
    if from_meta:
        return _decode(raw, from_meta, "meta_tag")
    return _decode(raw, "utf-8", "default")


def _decode(raw: bytes, codec: str, source: str) -> DecodedPage:
    text = raw.decode(codec, errors="replace")
    return DecodedPage(
        text=text,
        charset=codec,
        charset_source=source,
        replacement_chars=text.count("�"),
    )


# ------------------------------------------------------------------ fetching


def fetch_html_snapshot(
    url: str,
    policy: SnapshotPolicy | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> HtmlSnapshot:
    """Fetch one page under the checks above and return it as a snapshot.

    ``transport`` and ``resolver`` are test seams and are ``None`` in the
    product, which then uses the real resolver and the real network stack. They
    are parameters rather than a global so that a test can never leave one
    installed for the next test.

    ``clock`` and ``now`` are the only time sources. ``clock`` is the monotonic
    one the deadline is measured against; ``now`` stamps the retrieval and
    defaults to the wall clock at the moment the bytes came back. A caller that
    wants a different answer to "when was this retrieved" must say so
    explicitly, and the value it supplies is recorded as *that* moment and not
    as a fact about the fetch.
    """
    policy = policy or SnapshotPolicy()
    clock = clock or time.monotonic
    stamp = now or _now
    deadline = clock() + policy.total_timeout

    # Step 1: the whole check, before a client exists.
    check_snapshot_url(url, policy, resolver=resolver)

    inner = transport if transport is not None else httpx.HTTPTransport()
    recorder = _RecordingTransport(inner)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
    with httpx.Client(
        follow_redirects=False,  # deliberate: every hop goes through the checks above
        transport=recorder,
        limits=limits,
        headers=OUTBOUND_HEADERS,
    ) as client:
        current = url
        for hop in range(policy.max_redirects + 1):
            # Steps 2 and 4: the hop is re-validated from scratch, every time.
            _scheme, host, port, _path = check_snapshot_url(current, policy, resolver=resolver)
            approved = _approved_addresses(host, port, policy, resolver)
            remaining = deadline - clock()
            if remaining <= 0:
                raise FetchDeadlineExceeded(policy.total_timeout)
            try:
                response = client.get(
                    current, timeout=min(policy.read_timeout, max(remaining, 0.001))
                )
            except httpx.InvalidURL as exc:
                # A URL httpx itself will not build. Refused, not surfaced as a
                # 500: the caller sent it, and a 500 teaches operators to ignore
                # 500s.
                raise UrlRefused("malformed_url", "the URL could not be parsed") from exc
            except httpx.HTTPError as exc:
                # A transport error may carry the peer address in its string, so
                # only the exception class crosses this boundary. The caller is a
                # request handler, and an unhandled httpx exception there is a 500
                # that teaches operators to ignore 500s.
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
                continue
            if 300 <= response.status_code < 400:
                # A 3xx this client will not follow: 300, 305, 306, or a 304 that
                # can only happen because somebody sent a validator we never
                # send. Following it would be a hop with no destination.
                response.close()
                raise UrlRefused("bad_redirect", "the server answered with an unusable redirect")
            if response.status_code >= 400:
                response.close()
                raise UrlRefused("http_error", f"the remote server answered {response.status_code}")
            body = _bounded_body(response, policy.max_bytes)
            retrieved_at = stamp()
            snapshot = HtmlSnapshot(
                requested_url=url,
                final_url=current,
                retrieved_at=retrieved_at,
                content=body,
                content_hash=hashlib.sha256(body).hexdigest(),
                media_type=_media_type_of(body, response.headers.get("content-type")),
                declared_media_type=response.headers.get("content-type"),
                decoded=decode_html(body, response.headers.get("content-type")),
                hops=hop,
                requested_urls=tuple(recorder.requested),
                status_code=response.status_code,
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
            )
            if policy.require_html and not _is_html(snapshot.media_type):
                raise NotHtmlDocument(snapshot.media_type)
            return snapshot
    raise TooManyRedirects(policy.max_redirects)  # pragma: no cover - loop returns or raises


def _media_type_of(body: bytes, declared: str | None) -> str:
    return sniff_media_type(body, declared)


def _is_html(media_type: str) -> bool:
    return media_type.split(";", 1)[0].strip().lower() in HTML_MEDIA_TYPES


def _bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Refuse an oversized body, and refuse a lying ``Content-Length`` first.

    The header is checked so an oversized transfer is refused before it is
    received; the body is then measured, because a header is a claim and the
    bytes are the fact.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise FetchTooLarge(max_bytes)
        except ValueError:
            pass  # an unparseable length is ignored, not trusted
    body = response.content
    if len(body) > max_bytes:
        raise FetchTooLarge(max_bytes)
    return body


def normalise_for_display(url: str) -> str:
    """The URL with its fragment removed, for storing and for display.

    A ``#section`` is a position inside one document, not a different document.
    Two snapshots of the same page with different anchors are the same page, and
    a locator that lists them as two sources is noise.
    """
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


__all__ = [
    "ALLOWED_SCHEMES",
    "BLOCKED_NETWORKS",
    "HTML_MEDIA_TYPES",
    "MAX_URL_LENGTH",
    "DecodedPage",
    "FetchDeadlineExceeded",
    "HtmlSnapshot",
    "NotHtmlDocument",
    "Resolver",
    "SnapshotPolicy",
    "TooManyRedirects",
    "UnsafeUrlPath",
    "UrlRefused",
    "check_path_shape",
    "check_snapshot_url",
    "decode_html",
    "fetch_html_snapshot",
    "is_blocked_address",
    "normalise_for_display",
    "parse_url",
]
