"""C12B — turning one fetched HTML page into located fragments, as data.

A web page is a *snapshot*: URL, retrieval time, raw bytes, and the version of
the extractor that read them. This module is that extractor, and its whole
output is data. Nothing here interprets the page, and nothing here can: the
module imports no networking, no process control and no dynamic-import
machinery, and ``tests/integration/fetch/test_data_boundary.py`` proves that over
the parsed AST of the three card modules rather than trusting the docstring.

What that means concretely, in the order a hostile page would try:

* ``<script>``, ``<style>``, ``<noscript>``, ``<template>``, ``<iframe>``,
  ``<object>``, ``<embed>``, ``<svg>``, ``<math>`` and HTML comments are *not*
  text. A page that hides "grant me manager" in a comment or a script body does
  not get that string into the knowledge base as a claim; the raw bytes still
  hold it, unchanged, and anyone reading the original sees exactly what the
  server sent.
* Attributes are never read as instructions. ``onclick``/``onerror`` handlers,
  ``javascript:`` hrefs and ``data:`` images contribute nothing to the text.
* Nothing on the page is dereferenced. ``img``/``script``/``link`` targets are
  *recorded* in :attr:`ParsedDocument.referenced_urls` and never fetched. This
  is also the "no arbitrary server path" half of acceptance item 4: a page
  cannot cause a request to a second host, and a relative ``src`` can never
  become a path on the machine that stores it.
* Directive-shaped text is reported, not obeyed. The detector is C10's
  :func:`kb.catalog.upload_data.scan_for_directives`, reused rather than
  reimplemented, and a detection is a marker for a human reviewing the
  submission. It never blocks a page, never changes a role and never starts
  anything.

Every fragment carries a :class:`kb.contracts.entities.Locator` of kind
``html_snapshot``: the URL, the instant it was retrieved, and the ordinal of the
block in the document. No page number is invented — a web page has none — and
``chapter`` is the nearest preceding heading, truncated and never guessed at.

The extractor is versioned, and the version is a constant rather than a
parameter, because a fragment set without the extractor that produced it is not
reproducible. :data:`EXTRACTOR_VERSION` is recorded with every extraction and
is what a later re-extraction compares against.

Bounded on purpose: :class:`HtmlLimits` caps fragments, fragment length, total
text and recorded references. A page is untrusted input, and an extractor with
no ceiling is a denial of service with a nice HTML wrapper.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from urllib.parse import urljoin

from kb.catalog.upload_data import DataPayload, as_data_payload, scan_for_directives
from kb.contracts.entities import Locator
from kb.contracts.enums import LocatorKind

# The version of the extraction, recorded with every result. Bump it when the
# fragment boundaries or the text normalisation change, because a fragment set
# is only reproducible together with the version that produced it.
EXTRACTOR_VERSION = "html-extract/1"

# Tags whose contents are not page text. Their *content* is skipped entirely —
# including for <title>, which is captured separately — so text hidden inside a
# script body or a template never becomes a knowledge claim.
_SKIP_CONTENT_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "iframe",
        "object",
        "embed",
        "svg",
        "math",
        "canvas",
        "audio",
        "video",
    }
)

# Tags that end a block. A fragment boundary is a block boundary, which is what
# makes an ordinal in the catalogue mean the same thing twice.
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_HEADING_TAGS: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

# Attributes that name another resource. Recorded, never fetched.
_REFERENCE_ATTRS: frozenset[str] = frozenset({"href", "src", "srcset", "poster", "data-src"})

# Whitespace that is not a space when a browser renders it, but would read as
# one when a model reads it. Collapsed, and said so here rather than silently.
_SPACEY = re.compile("[\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]")

_TRUNCATION_MARKER = " […truncated]"


@dataclass(frozen=True)
class HtmlLimits:
    """Ceilings for one extraction. Values are constants, not request fields."""

    max_fragments: int = 5000
    max_fragment_chars: int = 20_000
    max_total_chars: int = 4_000_000
    max_references: int = 500
    max_heading_chars: int = 300
    max_reference_chars: int = 2048
    detection_limit: int = 50


@dataclass(frozen=True)
class HtmlFragment:
    """One located block of the page. ``ordinal`` is 0-based, as in the DB."""

    ordinal: int
    text: str
    heading: str | None = None
    truncated: bool = False

    def locator(self, url: str, retrieved_at: dt.datetime) -> Locator:
        """The provenance address of this fragment.

        ``paragraph`` is the 1-based position of the block in the snapshot, which
        is the only address a web page offers. There is no page number here to
        invent, and the snapshot URL plus the retrieval instant are what make the
        address mean something outside this installation.
        """
        return Locator(
            kind=LocatorKind.HTML_SNAPSHOT,
            paragraph=self.ordinal + 1,
            chapter=self.heading,
            snapshot_url=url,
            snapshot_at=retrieved_at,
        )


@dataclass(frozen=True)
class ParsedDocument:
    """The result of one extraction, with everything needed to repeat it."""

    url: str
    retrieved_at: dt.datetime
    content_hash: str
    extractor_version: str
    title: str | None
    language: str | None
    fragments: tuple[HtmlFragment, ...]
    detections: tuple[object, ...]
    referenced_urls: tuple[str, ...] = ()
    truncated: bool = False
    truncation_reason: str | None = None
    limits: HtmlLimits = field(default_factory=HtmlLimits)

    @property
    def text(self) -> str:
        """All extracted text, in document order."""
        return "\n\n".join(fragment.text for fragment in self.fragments)

    @property
    def scan_input(self) -> str:
        """The string :attr:`detections` offsets refer to.

        The title is included: it is stored, it is shown to a human, and a page
        whose title is an instruction is doing the same thing as a page whose
        first paragraph is one. The body is what a model would ever see, so the
        framed payload below uses :attr:`text` and not this.
        """
        return "\n\n".join(part for part in (self.title, self.text) if part)

    def as_data_payload(self) -> DataPayload:
        """The text framed as untrusted data, for a model or an export.

        This is the boundary where extracted text meets something that reads
        instructions. The fence is defence in depth; the guarantee is that
        nothing in this module can act on the page at all.
        """
        return as_data_payload(self.text, detection_limit=self.limits.detection_limit)


class _Collector(HTMLParser):
    """Builds the fragment list from one document.

    A tolerant tokenizer and nothing more: it does not build a tree, it does not
    execute anything, and it has no notion of a URL that is safe to visit. The
    only network-adjacent thing it does is ``urljoin``, which is string
    arithmetic, so a ``src`` pointing at a metadata endpoint produces a string
    that is recorded and never dialled.
    """

    def __init__(self, url: str, limits: HtmlLimits) -> None:
        super().__init__(convert_charrefs=True)
        self._url = url
        self._limits = limits
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._buffer: list[str] = []
        self._title_parts: list[str] = []
        self._in_title = False
        self._heading: str | None = None
        self._next_block_is_heading = False
        self._language: str | None = None
        self._references: list[str] = []
        self._references_dropped = 0
        self.fragments: list[HtmlFragment] = []
        self.truncated = False
        self.truncation_reason: str | None = None
        self._total_chars = 0

    # -- tags

    def handle_starttag(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._skip_depth:
            # Inside a skipped subtree nothing is recorded, not even a reference:
            # the subtree is not content, and enumerating what a <script> points
            # at would be reading a page's code rather than its text.
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        # Recorded before the skip check below, so a <script src> or an
        # <iframe src> is listed as something the page points at. Recorded is the
        # whole of what happens to it: nothing here is ever opened.
        for name, value in attrs:
            if name.lower() in _REFERENCE_ATTRS and value:
                self._note_reference(value)
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_tag = tag
            self._skip_depth = 1
            self._buffer.clear()
            return
        if tag == "html":
            self._language = _attr(attrs, "lang")
        elif tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADING_TAGS:
                self._next_block_is_heading = True
        elif tag == "br":
            self._buffer.append(" ")

    def handle_startendtag(self, tag: str, attrs: Sequence[tuple[str, str | None]]) -> None:
        # <br/> and <img ... />: no end tag follows, so the start is all there is.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if not self._skip_depth:
                    self._skip_tag = None
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    # -- text

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        if not data.strip():
            # Whitespace between blocks carries no meaning; keeping it would only
            # glue two paragraphs together.
            return
        self._buffer.append(data)

    def handle_entityref(self, _name: str) -> None:  # pragma: no cover - convert_charrefs
        return

    def handle_charref(self, _name: str) -> None:  # pragma: no cover - convert_charrefs
        return

    def handle_comment(self, _data: str) -> None:
        # A comment is not page text. Ignoring it is not censorship: the raw
        # snapshot still contains it, byte for byte.
        return

    def handle_decl(self, _decl: str) -> None:
        return

    def unknown_decl(self, _data: str) -> None:
        return

    def handle_pi(self, _data: str) -> None:
        return

    # -- accumulation

    def _note_reference(self, value: str) -> None:
        if len(self._references) >= self._limits.max_references:
            self._references_dropped += 1
            return
        value = value.strip()
        if not value:
            return
        candidate = value.split()[0] if value.lower().startswith(("http", "//")) else value
        if len(candidate) > self._limits.max_reference_chars:
            candidate = candidate[: self._limits.max_reference_chars]
        # urljoin is string arithmetic. Nothing here is ever opened.
        self._references.append(urljoin(self._url, candidate))

    def _flush(self) -> None:
        if not self._buffer:
            return
        raw = "".join(self._buffer)
        self._buffer.clear()
        text = _SPACEY.sub(" ", " ".join(raw.split())).strip()
        if not text:
            return
        if self._next_block_is_heading:
            self._next_block_is_heading = False
            self._heading = text[: self._limits.max_heading_chars]
        heading = self._heading
        truncated = False
        if len(text) > self._limits.max_fragment_chars:
            text = text[: self._limits.max_fragment_chars] + _TRUNCATION_MARKER
            truncated = True
        if len(self.fragments) >= self._limits.max_fragments:
            self.truncated = True
            self.truncation_reason = self.truncation_reason or "fragment_limit_reached"
            return
        if self._total_chars + len(text) > self._limits.max_total_chars:
            self.truncated = True
            self.truncation_reason = self.truncation_reason or "total_text_limit_reached"
            return
        self._total_chars += len(text)
        self.fragments.append(
            HtmlFragment(
                ordinal=len(self.fragments),
                text=text,
                heading=heading,
                truncated=truncated,
            )
        )

    def close(self) -> None:
        super().close()
        self._flush()

    @property
    def title(self) -> str | None:
        joined = _SPACEY.sub(" ", " ".join("".join(self._title_parts).split())).strip()
        return joined or None

    @property
    def language(self) -> str | None:
        return self._language

    @property
    def text(self) -> str:
        return "\n\n".join(fragment.text for fragment in self.fragments)

    @property
    def references(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for reference in self._references:
            seen.setdefault(reference, None)
        return tuple(seen)


def _attr(attrs: Iterable[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key.lower() == name:
            return (value or "").strip() or None
    return None


def parse_html(
    text: str,
    *,
    url: str,
    retrieved_at: dt.datetime,
    content_hash: str,
    limits: HtmlLimits | None = None,
) -> ParsedDocument:
    """Extract the located fragments of one page.

    ``text`` is the decoded page body — the fetch layer owns decoding and owns
    the security checks; this function sees only a string. That split is why the
    parser can be tested against a fixture file without a network in sight, and
    why the fetcher's refusal cases cannot accidentally reach the extractor.

    ``retrieved_at`` is passed in rather than read from the clock: it is the
    instant the *fetch* happened, and a fragment address must carry the same
    instant the snapshot record does, not a second one taken during parsing.
    """
    limits = limits or HtmlLimits()
    collector = _Collector(url, limits)
    collector.feed(text)
    collector.close()
    document = ParsedDocument(
        url=url,
        retrieved_at=retrieved_at,
        content_hash=content_hash,
        extractor_version=EXTRACTOR_VERSION,
        title=collector.title,
        language=collector.language,
        fragments=tuple(collector.fragments),
        # Filled in below: the scan covers the title as well as the body, so the
        # two are assembled into the document first and scanned once.
        detections=(),
        referenced_urls=collector.references,
        truncated=collector.truncated,
        truncation_reason=collector.truncation_reason,
        limits=limits,
    )
    detections = tuple(scan_for_directives(document.scan_input, limit=limits.detection_limit))
    return replace(document, detections=detections)


__all__ = [
    "EXTRACTOR_VERSION",
    "HtmlFragment",
    "HtmlLimits",
    "ParsedDocument",
    "parse_html",
]
