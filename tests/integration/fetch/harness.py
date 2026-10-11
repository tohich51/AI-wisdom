"""C12B — the test harness: fetch, extract, or both, with the policy still on.

Kept out of ``conftest.py`` for the same reason ``provisioning/doubles.py`` is:
the helpers are documentation, and a test that imports them is stating which
arrangement it is using.

What is real and what is faked, once, here rather than in every test:

* the SSRF checks are real and they run on every path through this module;
* the address rules belong to C10 (``kb.catalog.upload_fetch``) and this card
  calls them rather than re-implementing them;
* the transport is a ``httpx.MockTransport``, so the redirect chain can be driven
  from a host that is not private. The client, the loop, the response objects and
  every check are real; only the socket is absent;
* the resolver is injected and answers only what a test declares, defaulting to
  loopback so an undeclared host gets a refusal rather than an accidental pass;
* ``FROZEN_NOW`` is the retrieval instant, injected, so a golden comparison is
  not a comparison of clocks. Nothing here ever fetches from the internet.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import httpx

from kb.catalog.fetch_html import HtmlSnapshot, SnapshotPolicy, fetch_html_snapshot
from kb.catalog.parsers.html_extract import ParsedDocument, parse_html

# A real, routable address standing in for "a host that resolves to the
# internet". It is never dialled: every case using it is refused before a
# connection, or answered by the mock transport.
PUBLIC = "93.184.216.34"

# The retrieval instant every helper stamps, so two snapshots taken in one test
# differ only where the test wants them to differ.
FROZEN_NOW = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)

Resolver = Callable[[str, int], list[str]]

# PENDING DDL. migrations/ has a single owner and this card may not add to it.
#
# kb.fragment has RLS enabled and forced with a SELECT policy and no write
# policy, so kb_app cannot INSERT a fragment at all — found by running the
# insert, not by reading the migration. The conftest applies the policy below so
# the rest of the suite can run, and two tests assert both that it is absent
# from the repository and that without it the insert fails loudly rather than
# silently reporting a success. The narrow shape is the one C10 used for
# kb.source_version in 0004_uploads.sql: INSERT only, no UPDATE and no DELETE.
PENDING_FRAGMENT_WRITE_POLICY = """
DROP POLICY IF EXISTS fragment_write ON kb.fragment;
CREATE POLICY fragment_write ON kb.fragment FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM kb.source s
        WHERE s.id = source_id
          AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
    ));
"""


def resolver_for(mapping: dict[str, list[str]]) -> Resolver:
    """A resolver that answers only what the test declares.

    Anything undeclared resolves to loopback, so a test that forgot to declare a
    host gets a refusal rather than an accidental pass.
    """

    def _resolve(host: str, _port: int) -> list[str]:
        return list(mapping.get(host, ["127.0.0.1"]))

    return _resolve


def public_resolver(*hosts: str) -> Resolver:
    """A resolver that answers ``PUBLIC`` for the named hosts and loopback otherwise."""
    return resolver_for({host: [PUBLIC] for host in hosts})


def snapshot_over(
    transport: httpx.BaseTransport,
    *,
    resolver: Resolver | None = None,
    url: str = "http://public.test/article",
    policy: SnapshotPolicy | None = None,
    now: Callable[[], dt.datetime] | None = None,
    clock: Callable[[], float] | None = None,
) -> HtmlSnapshot:
    """A snapshot taken over a ``MockTransport``, with the policy still in force."""
    return fetch_html_snapshot(
        url,
        policy or SnapshotPolicy(),
        transport=transport,
        resolver=resolver if resolver is not None else resolver_for({"public.test": [PUBLIC]}),
        now=now or (lambda: FROZEN_NOW),
        clock=clock,
    )


def parsed_from(snapshot: HtmlSnapshot) -> ParsedDocument:
    """The extraction of a snapshot, with the same URL, instant and hash."""
    return parse_html(
        snapshot.text,
        url=snapshot.final_url,
        retrieved_at=snapshot.retrieved_at,
        content_hash=snapshot.content_hash,
    )


def serving(
    content: bytes, *, content_type: str = "text/html; charset=utf-8"
) -> httpx.MockTransport:
    """A transport that answers every request with the same bytes."""
    return httpx.MockTransport(
        lambda _request: httpx.Response(
            200, content=content, headers={"Content-Type": content_type}
        )
    )


def routing(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    """A transport that answers by path. A path that is not in the map is a 404."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return routes.get(request.url.path, httpx.Response(404))

    return httpx.MockTransport(_handler)
