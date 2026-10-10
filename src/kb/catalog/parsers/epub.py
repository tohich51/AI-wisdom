"""EPUB → normalised fragments with exact structural locators (card C11).

An EPUB has **no pages**. It is a ZIP of XHTML documents in a declared reading
order (the *spine*), and its smallest honest address is a document plus a
position inside that document. So nothing in this module produces a file page
or a printed label, ever: ``file_page`` and ``printed_label`` are ``None`` on
every fragment, and a book that is "page 12" of some other edition has no way
to express that here — which is the correct answer, not a gap to be filled
with a guess.

What each fragment carries:

* ``locator.kind = epub_chapter`` — the spine document it came from.
* ``locator.spine`` — the document's 1-based position in the spine, read from
  the OPF ``<spine>``. The string is the schema's own type; the value is an
  index that was read, not computed from anything else.
* ``locator.chapter`` — the title the book's own table of contents gives for
  that document, or ``None`` when the book states no title for it. A missing
  title is not filled in from the file name, from the first heading, or from
  the previous chapter.
* ``locator.paragraph`` — 1-based position among the document's text blocks.

The container is read by **EbookLib** (see ``PARSER_VERSION``), a real library
reading the real bytes. XHTML bodies are turned into ordered text blocks by the
standard library's ``html.parser``. There is no OCR: a book that is a pile of
images inside a container comes back ``needs_ocr`` with no fragments, because
full OCR/vision is not promised in v1 and a fabricated first sentence is worse
than an honest empty result.
"""

from __future__ import annotations

import os
import tempfile
import uuid
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from uuid import UUID

import ebooklib
from ebooklib import epub

from kb.contracts.entities import Fragment, Locator
from kb.contracts.enums import LocatorKind, ProcessingStatus

__all__ = [
    "PARSER_NAME",
    "PARSER_VERSION",
    "EpubParseResult",
    "EpubProblem",
    "fragment_row",
    "parse_epub",
    "text_at_locator",
    "text_blocks",
]

PARSER_NAME = "ebooklib"
PARSER_VERSION: str = ".".join(str(part) for part in getattr(ebooklib, "VERSION", ()))

MEDIA_TYPE = "application/epub+zip"

#: Elements whose text is a unit of reading. Table cells count separately: a
#: cell is the smallest addressable piece of text in an HTML table, and joining
#: cells into one string would lose which cell said what.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "li",
        "blockquote",
        "pre",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "td",
        "th",
        "dt",
        "dd",
        "figcaption",
    }
)

#: Elements that carry no body text. ``head``/``title`` is document metadata,
#: ``nav`` is the table of contents, and script/style text is not the book.
_IGNORED_TAGS = frozenset({"head", "title", "script", "style", "nav", "svg"})


@dataclass(frozen=True, slots=True)
class EpubProblem:
    """A visible, named reason the parse is not a clean success."""

    code: str
    detail: str
    spine: str | None = None


@dataclass(frozen=True, slots=True)
class EpubParseResult:
    """Everything one parse produced, including what it could not produce."""

    parser: str
    parser_version: str
    media_type: str
    processing: ProcessingStatus
    spine_documents: int
    fragments: tuple[Fragment, ...]
    problems: tuple[EpubProblem, ...]
    title: str | None = None
    author: str | None = None

    @property
    def needs_ocr(self) -> bool:
        return self.processing is ProcessingStatus.NEEDS_OCR

    @property
    def chapters(self) -> tuple[str | None, ...]:
        return tuple(f.locator.chapter for f in self.fragments)


def parse_epub(data: bytes, *, source_id: UUID) -> EpubParseResult:
    """Parse an EPUB into one fragment per text block, in reading order."""
    try:
        book = _read(data)
    except Exception as exc:
        return _failure(
            f"EbookLib {PARSER_VERSION} could not open the container: {exc}",
        )

    spine_entries = _spine_entries(book)
    if not spine_entries:
        return _failure(
            "the package declares an empty spine; there is no document to read "
            "and 'needs_ocr' would describe a scanned page that does not exist",
            code="empty_spine",
        )

    titles = _toc_titles(book)
    fragments: list[Fragment] = []
    problems: list[EpubProblem] = []
    documents_with_text = 0

    for position, (idref, _linear) in enumerate(spine_entries, start=1):
        spine_label = str(position)
        item = None
        try:
            item = book.get_item_with_id(idref)
        except Exception as exc:
            problems.append(
                EpubProblem(
                    code="unreadable_spine_item",
                    detail=f"spine entry {spine_label} ({idref}) could not be read: {exc}",
                    spine=spine_label,
                )
            )
            continue
        if item is None:
            problems.append(
                EpubProblem(
                    code="missing_spine_item",
                    detail=(
                        f"spine entry {spine_label} names manifest id {idref!r}, which "
                        "the manifest does not define"
                    ),
                    spine=spine_label,
                )
            )
            continue

        href = getattr(item, "file_name", None)
        blocks = _blocks_of(item)
        if not blocks:
            problems.append(
                EpubProblem(
                    code="no_text_in_spine_item",
                    detail=(
                        f"spine entry {spine_label} ({href}) holds no text; v1 does "
                        "not promise OCR, so no text was produced for it"
                    ),
                    spine=spine_label,
                )
            )
            continue

        documents_with_text += 1
        chapter = titles.get(_match_key(href)) if href else None
        for paragraph, text in enumerate(blocks, start=1):
            fragments.append(
                Fragment(
                    id=uuid.uuid4(),
                    source_id=source_id,
                    ordinal=len(fragments),
                    locator=Locator(
                        kind=LocatorKind.EPUB_CHAPTER,
                        spine=spine_label,
                        chapter=chapter,
                        paragraph=paragraph,
                    ),
                    text=text,
                )
            )

    if documents_with_text == 0:
        # Images in a container. No text exists, so none is written down.
        processing = ProcessingStatus.NEEDS_OCR
    elif documents_with_text < len(spine_entries):
        processing = ProcessingStatus.PARTIAL
    else:
        processing = ProcessingStatus.DONE

    return EpubParseResult(
        parser=PARSER_NAME,
        parser_version=PARSER_VERSION,
        media_type=MEDIA_TYPE,
        processing=processing,
        spine_documents=len(spine_entries),
        fragments=tuple(fragments),
        problems=tuple(problems),
        title=_metadata(book, "title"),
        author=_metadata(book, "creator"),
    )


def text_at_locator(data: bytes, locator: Locator) -> str | None:
    """Re-open the book and return the text this locator points at.

    ``None`` means the locator does not name one addressable block: a PDF
    kind, a missing or non-numeric spine position, or a paragraph the document
    does not have. An EPUB locator is never satisfied by a page number, and a
    page number is never invented to satisfy one.
    """
    if locator.kind not in (LocatorKind.EPUB_CHAPTER, LocatorKind.EPUB_SPINE):
        return None
    position = _spine_position(locator.spine)
    if position is None:
        return None
    for fragment in parse_epub(data, source_id=uuid.UUID(int=0)).fragments:
        if fragment.locator.paragraph != locator.paragraph:
            continue
        if _spine_position(fragment.locator.spine) == position:
            return fragment.text
    return None


def fragment_row(fragment: Fragment) -> dict[str, Any]:
    """The ``kb.fragment`` column values for one parsed fragment.

    ``Locator.spine`` has no column in ``kb.fragment`` — the table stores
    ``locator_kind``, ``file_page``, ``printed_label``, ``chapter`` and
    ``paragraph``. The spine position is therefore *not* persisted by this
    mapping; it stays on the in-memory locator. That is a real hole in the
    schema rather than a choice, and it is reported to the single DDL owner
    instead of being worked around by overloading ``chapter`` with something
    that is not a chapter.
    """
    return {
        "id": fragment.id,
        "source_id": fragment.source_id,
        "ordinal": fragment.ordinal,
        "locator_kind": fragment.locator.kind.value,
        "file_page": fragment.locator.file_page,
        "printed_label": fragment.locator.printed_label,
        "chapter": fragment.locator.chapter,
        "paragraph": fragment.locator.paragraph,
        "text": fragment.text,
    }


def text_blocks(xhtml: str) -> tuple[str, ...]:
    """The document's text blocks, in document order.

    A block ends when its element ends or when another block element starts,
    whichever comes first, so a malformed document that never closes its
    ``<p>`` still yields blocks in the right order instead of one run-on.
    Empty blocks are dropped: whitespace between tags is layout, not text.
    """
    parser = _BlockExtractor()
    parser.feed(xhtml)
    parser.close()
    return tuple(parser.blocks)


# --------------------------------------------------------------------- bits


def _read(data: bytes) -> Any:
    """Hand the bytes to EbookLib, which takes a file rather than a stream."""
    handle = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
    try:
        handle.write(data)
        handle.close()
        with warnings.catch_warnings():
            # EbookLib asks for ignore_ncx=True by default in a future release.
            # NCX reading is switched ON here on purpose — many real EPUB2 books
            # carry their table of contents only in the NCX, and ignoring it
            # would turn a stated chapter title into a None. This is the one
            # warning suppressed, and only around this call.
            warnings.filterwarnings("ignore", message=".*ignore_ncx.*", category=UserWarning)
            return epub.read_epub(handle.name, options={"ignore_ncx": False})
    finally:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - the file is ours and it is gone
            pass


def _spine_entries(book: Any) -> list[tuple[str, Any]]:
    """``[(manifest id, linear flag)]`` in the order the OPF declares."""
    entries: list[tuple[str, Any]] = []
    for entry in getattr(book, "spine", []) or []:
        if isinstance(entry, (tuple, list)) and entry:
            entries.append((str(entry[0]), entry[1] if len(entry) > 1 else "yes"))
        elif entry is not None:
            entries.append((str(entry), "yes"))
    return entries


def _toc_titles(book: Any) -> dict[str, str]:
    """``{document key: title}`` from the book's own table of contents.

    Only a title the book actually states is recorded. A spine document the
    TOC does not mention is simply absent from this map, and its ``chapter``
    stays ``None``.
    """
    titles: dict[str, str] = {}
    for entry in _flatten_toc(getattr(book, "toc", []) or []):
        href, title = entry
        if not href or not title:
            continue
        key = _match_key(href)
        titles.setdefault(key, title.strip())
    return titles


def _flatten_toc(node: Any) -> Iterable[tuple[str | None, str | None]]:
    """EPUB3 nav links and EPUB2 NCX entries, in either nesting shape."""
    for entry in node:
        if isinstance(entry, (tuple, list)) and len(entry) == 2 and isinstance(entry[1], str):
            yield entry[0], entry[1]
        elif hasattr(entry, "href"):
            yield getattr(entry, "href", None), getattr(entry, "title", None)
        if hasattr(entry, "sections"):
            yield from _flatten_toc(getattr(entry, "sections", []) or [])


def _match_key(href: str | None) -> str:
    """A key that survives a relative or anchored href.

    A nav entry may point at ``ch2.xhtml`` while the manifest calls the same
    document ``OEBPS/ch2.xhtml``, and it may carry an ``#anchor``. Both are
    reduced to the trailing file name. Two documents sharing a file name would
    collide, which loses a title rather than inventing one.
    """
    if not href:
        return ""
    return Path(str(href).split("#", 1)[0]).name


def _spine_position(spine: str | None) -> int | None:
    """The spine position, when the locator names one that can exist."""
    if spine is None:
        return None
    try:
        position = int(str(spine).strip())
    except ValueError:
        return None
    return position if position >= 1 else None


def _blocks_of(item: Any) -> tuple[str, ...]:
    content = getattr(item, "get_content", None)
    if content is None:
        return ()
    try:
        raw = content()
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    except Exception:
        return ()
    return text_blocks(text)


def _metadata(book: Any, key: str) -> str | None:
    """A Dublin Core value, or ``None`` when the package states none.

    A creator list is joined in the order the package states it. An absent
    creator is ``None`` — never the submitter, the file name, or a placeholder.
    """
    try:
        # EbookLib returns (value, attributes) pairs, in the order the package
        # states them.
        entries = book.get_metadata("DC", key) or []
    except Exception:
        return None
    values = [str(value).strip() for value, _attrs in entries if str(value).strip()]
    if not values:
        return None
    return "; ".join(values) if len(values) > 1 else values[0]


def _failure(detail: str, *, code: str = "unreadable_epub") -> EpubParseResult:
    return EpubParseResult(
        parser=PARSER_NAME,
        parser_version=PARSER_VERSION,
        media_type=MEDIA_TYPE,
        processing=ProcessingStatus.FAILED,
        spine_documents=0,
        fragments=(),
        problems=(EpubProblem(code=code, detail=detail),),
    )


class _BlockExtractor(HTMLParser):
    """Collects the text of block-level elements, in the order they appear."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buffer: list[str] | None = None
        self._ignored: list[str] = []

    # -- tags
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._ignored:
            if tag in _IGNORED_TAGS:
                self._ignored.append(tag)
            return
        if tag in _IGNORED_TAGS:
            self._ignored.append(tag)
            return
        if tag in _BLOCK_TAGS:
            # A block inside a block closes the outer one out rather than
            # nesting, so the emitted order is the document order.
            self._flush()
            self._buffer = []
        elif tag == "br" and self._buffer is not None:
            self._buffer.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self._ignored and self._buffer is not None:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._ignored or self._buffer is None:
            return
        self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    # -- internals
    def _flush(self) -> None:
        if self._buffer is None:
            return
        text = " ".join("".join(self._buffer).split())
        self._buffer = None
        if text:
            self.blocks.append(text)
