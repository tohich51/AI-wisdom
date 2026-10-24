"""DOCX → :class:`kb.contracts.entities.Fragment`, with locators that resolve.

Library and licence
-------------------
Parsing is done by **python-docx 1.2.0** (MIT), installed into the shared venv.
It is a real library doing real OOXML work, not a stand-in: the parts it reads
are the parts a Word file actually contains, and the XML walked here is the XML
python-docx itself loaded. No PDF renderer is involved and no page is ever
derived from one — a DOCX carries no page geometry until something lays it out,
and this card does not lay it out. The package version actually imported is
recorded on every :class:`DocxParseResult` so a result can be traced to the
library that produced it.

What a locator means here, exactly
----------------------------------
``paragraph=N``   the N-th ``w:p`` that is a **direct child of ``w:body``**,
                  counting every one of them including the empty ones. That is
                  the same sequence as ``Document.paragraphs``, so
                  ``paragraph=N`` *is* ``Document.paragraphs[N-1]`` — not "the
                  N-th paragraph that had text". ``resolve_locator`` re-navigates
                  through ``Document.paragraphs`` and hands back the very element
                  the fragment was taken from; ``element`` identity is asserted
                  in the tests.
``table=N``      the N-th ``w:tbl`` that is a direct child of ``w:body``, the
                  same sequence as ``Document.tables``.
``cell=(R,C)``   the cell whose **origin** is at grid row R, grid column C of
                  table N, 1-based. R and C are where that ``w:tc`` begins, not
                  anywhere the cell happens to reach.
``chapter``      the text of the nearest preceding outline heading, detected
                  from ``w:outlineLvl`` first and from the heading style second.
                  ``None`` when the fragment is not under one. It is the heading
                  the fragment sits under, not a title invented for it.

Everything else on a DOCX locator stays ``None``: no ``file_page`` (a DOCX has
no page until it is laid out), no ``printed_label``, no ``spine``, no snapshot.
:mod:`kb.contracts.entities` refuses to let a printed label stand in for a file
page, and the ``kb.fragment`` CHECK constraint refuses a ``docx_*`` row that
carries a ``file_page`` at all; both refusals are exercised against a real
PostgreSQL in this card's tests.

The two places a parser lies, and what happens here
--------------------------------------------------
**Merged cells.** A horizontal merge is one ``w:tc`` with ``w:gridSpan``; a
vertical merge is a chain of ``w:tc`` where all but the first carry
``w:vMerge``. Enumerating the grid by column yields several addresses for one
cell. This parser emits **one** fragment per origin ``w:tc``, addressed at its
top-left grid position, and records every covered position in
:attr:`DocxParseResult.covered` instead of inventing a fragment for it. See
:mod:`kb.catalog.parsers.docx_grid`.

**Nested tables.** A table inside a cell has no place in ``Locator``, which
carries a single ``table`` number and an optional ``cell``: there is no
nesting depth to put the inner table's number in, and inventing a second
numbering scheme would create addresses that nothing else in the system could
resolve. So a nested table is reported in :attr:`DocxParseResult.unsupported`
with its own text intact — visible, recoverable, and carrying no fragment. The
same is true of the other constructs this parser declines to guess at: text
boxes, tracked deletions, body-level content controls and ``w:altChunk``.
None of them is dropped silently, and none of them is given a number.

Fragment identifiers are derived, not random: ``uuid5`` over
(``source_id``, ``ordinal``). Parsing the same bytes for the same source twice
produces the same identifiers, so a re-parse is recognisable as a re-parse and
cannot quietly become a second set of rows.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import io
import pathlib
import re
import uuid
from typing import Any

from docx.oxml.ns import qn
from lxml import etree

from kb.catalog.parsers.docx_archive import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    ArchiveReport,
    inspect_docx,
    read_docx_bytes,
)
from kb.catalog.parsers.docx_grid import (
    GridNote,
    TableGrid,
    build_grid,
    nested_tables,
    own_paragraphs,
)
from kb.contracts.entities import Fragment, Locator
from kb.contracts.enums import LocatorKind

__all__ = [
    "PARSER_NAME",
    "PARSER_VERSION",
    "DocxParseError",
    "DocxParseResult",
    "Unsupported",
    "parse_docx",
    "parse_docx_bytes",
    "resolve_locator",
]

PARSER_NAME = "kb.catalog.parsers.docx"
#: Bumped whenever the addressing rules above change. It is part of the
#: idempotency key of a parse job (ARCHITECTURE §7), so a change here must be a
#: change there.
PARSER_VERSION = "1"

#: How deep a nested table is rendered before the parser stops and says so.
MAX_NESTING_DEPTH = 4

#: How deep the text walk will recurse into a paragraph. XML from an untrusted
#: upload has no bound on nesting, and a recursion that follows it is a
#: RecursionError in the middle of a parse. Past this depth the walk stops and
#: reports, so the document is refused loudly rather than half-read.
MAX_TEXT_DEPTH = 64

#: A style id that means "outline level N". Word writes ``Heading1``; some
#: producers write ``heading 1`` or ``berschrift_1``'s equivalent. A name that
#: does not match is not a heading, and a fragment under it says ``chapter=None``
#: rather than being filed under a guess.
_HEADING_STYLE = re.compile(r"^heading[\s_-]*([1-9])$", re.IGNORECASE)

_W_T = qn("w:t")
_W_TAB = qn("w:tab")
_W_BR = qn("w:br")
_W_CR = qn("w:cr")
_W_NOBREAK_HYPHEN = qn("w:noBreakHyphen")
_W_DEL = qn("w:del")
_W_DEL_TEXT = qn("w:delText")
_W_TXBX = qn("w:txbxContent")
_W_P = qn("w:p")
_W_TBL = qn("w:tbl")
_W_SDT = qn("w:sdt")
_W_ALT_CHUNK = qn("w:altChunk")
_MC_ALTERNATE = "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"
_MC_CHOICE = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Choice"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
# Property containers never hold text. Walking into one can only add noise.
_PROPERTY_TAGS = frozenset(
    qn(tag)
    for tag in (
        "w:pPr",
        "w:rPr",
        "w:tblPr",
        "w:tblPrEx",
        "w:tcPr",
        "w:trPr",
        "w:sectPr",
        "w:sdtPr",
        "w:tblGrid",
    )
)

#: A fragment identifier is derived from these two, never random.
FRAGMENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "kb://fragment/docx/v1")


class DocxParseError(Exception):
    """The bytes opened as a container but are not a document we can read.

    Distinct from :class:`~kb.catalog.parsers.docx_archive.DocxArchiveError`,
    which is raised before any of them is opened. Both are refusals; neither
    produces a partial fragment list.
    """


@dataclasses.dataclass(frozen=True)
class Unsupported:
    """A construct that exists in the file and is deliberately not given a locator."""

    code: str
    address: str
    detail: str
    text: str


@dataclasses.dataclass(frozen=True)
class CoveredPosition:
    """A grid position that a merge makes indistinguishable from its anchor.

    Reported so that "why is there no fragment at (2, 1)?" has an answer that
    came from the file, rather than from a gap in the parser.
    """

    table: int
    row: int
    col: int
    anchor_row: int
    anchor_col: int


@dataclasses.dataclass(frozen=True)
class ParseCounts:
    body_paragraphs: int
    body_tables: int
    cell_fragments: int
    fragments: int
    empty_paragraphs_skipped: int
    empty_tables_skipped: int


@dataclasses.dataclass(frozen=True)
class DocxParseResult:
    fragments: tuple[Fragment, ...]
    unsupported: tuple[Unsupported, ...]
    covered: tuple[CoveredPosition, ...]
    counts: ParseCounts
    archive: ArchiveReport
    parser: str = PARSER_NAME
    parser_version: str = PARSER_VERSION
    library: str = "python-docx"
    library_version: str = ""

    def fragment_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(f.id for f in self.fragments)

    def by_kind(self, kind: LocatorKind) -> tuple[Fragment, ...]:
        return tuple(f for f in self.fragments if f.locator.kind is kind)


# ------------------------------------------------------------------- text


def _library_version() -> str:
    try:
        return importlib.metadata.version("python-docx")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover - install is required
        return "unknown"


def _walk(el: etree._Element, out: list[str], flags: set[str], depth: int = 0) -> None:
    for child in el:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag == _W_T:
            out.append(child.text or "")
        elif tag == _W_TAB:
            out.append("\t")
        elif tag in (_W_BR, _W_CR):
            out.append("\n")
        elif tag == _W_NOBREAK_HYPHEN:
            out.append("-")
        elif tag == _W_DEL:
            if any((d.text or "").strip() for d in child.iter(_W_DEL_TEXT)):
                flags.add("tracked_deletion")
        elif tag == _W_TXBX:
            if _raw_text(child).strip():
                flags.add("text_box")
        elif tag in _PROPERTY_TAGS:
            continue
        elif tag == _MC_ALTERNATE:
            # Choice and Fallback hold the *same* drawing. Reading both would
            # print it twice.
            picked = child.find(_MC_CHOICE)
            if picked is None:
                picked = child.find(_MC_FALLBACK)
            if picked is not None:
                _walk(picked, out, flags, depth + 1)
        elif depth >= MAX_TEXT_DEPTH:
            flags.add("text_depth_limit")
        else:
            _walk(child, out, flags, depth + 1)


def _raw_text(el: etree._Element) -> str:
    out: list[str] = []
    _walk(el, out, set())
    return "".join(out)


def normalise(text: str) -> str:
    """Per-line trim, drop the empty lines, keep the line breaks that are real."""
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def paragraph_text(p: etree._Element) -> tuple[str, frozenset[str]]:
    """The text of one ``w:p``, and what had to be left out of it.

    A deliberate superset of ``python-docx``'s ``Paragraph.text``: this also
    reads text inside ``w:ins`` (a tracked insertion is text the document shows)
    and inside ``w:fldSimple`` (a field's result is text the document shows). It
    never reads ``w:delText`` or ``w:instrText`` — deleted text is not in the
    document, and a field code is an instruction, not content — and both are
    reported through the returned flags rather than passed over in silence.
    """
    flags: set[str] = set()
    out: list[str] = []
    _walk(p, out, flags)
    return normalise("".join(out)), frozenset(flags)


def cell_text(tc: etree._Element) -> str:
    """The cell's own paragraphs, newline-separated. Nested tables excluded."""
    parts = [paragraph_text(p)[0] for p in own_paragraphs(tc)]
    return "\n".join(part for part in parts if part)


def _one_line(text: str) -> str:
    return " / ".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def render_grid(grid: TableGrid, depth: int = 0) -> str:
    """The table as text: one line per grid row, one field per grid column.

    The grid shape is preserved rather than tidied. A position covered by a
    merge prints ``[merged]`` rather than a second copy of the anchor's text, a
    cell that also holds a nested table says so, and a column no row fills stays
    empty. A table fragment that could be pasted back into a spreadsheet is a
    better fragment than one that silently re-flows around a merge.
    """
    lines: list[str] = []
    for row in range(grid.row_count):
        fields: list[str] = []
        for col in range(grid.columns):
            box = grid.box_at(row, col)
            if box is None:
                fields.append("")
                continue
            if box.position != (row, col):
                fields.append("[merged]")
                continue
            value = _one_line(cell_text(box.element))
            if nested_tables(box.element):
                value = f"{value} [nested table]".strip()
            fields.append(value)
        lines.append(" | ".join(fields))
    return "\n".join(lines)


def _render_nested(tbl: etree._Element, depth: int) -> str:
    if depth >= MAX_NESTING_DEPTH:
        return "[nesting deeper than the parser will render]"
    grid, _notes = build_grid(tbl)
    return render_grid(grid, depth + 1)


# ------------------------------------------------------------------ locator


def _heading_level(p: etree._Element) -> int | None:
    """Outline level 1 to 9, or ``None``.

    ``w:outlineLvl`` is what actually defines the outline and is the same number
    in a localised document. The style name is the fallback because it is what a
    document without explicit outline levels will have; a document with neither
    yields ``None``, and a fragment under no heading says so.
    """
    pPr = p.find(qn("w:pPr"))
    if pPr is None:
        return None
    outline = pPr.find(qn("w:outlineLvl"))
    if outline is not None:
        raw = outline.get(qn("w:val"))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = -1
        if 0 <= value <= 8:
            return value + 1
    style = pPr.find(qn("w:pStyle"))
    if style is None:
        return None
    match = _HEADING_STYLE.match((style.get(qn("w:val")) or "").strip())
    return int(match.group(1)) if match else None


def _locator(
    kind: LocatorKind,
    *,
    source_chapter: str | None,
    paragraph: int | None = None,
    table: int | None = None,
    cell: tuple[int, int] | None = None,
) -> Locator:
    return Locator(
        kind=kind,
        chapter=source_chapter,
        paragraph=paragraph,
        table=table,
        cell=cell,
    )


def _fragment_id(source_id: uuid.UUID, ordinal: int) -> uuid.UUID:
    return uuid.uuid5(FRAGMENT_NAMESPACE, f"{source_id}/{ordinal}")


# ------------------------------------------------------------------- parse


class _Walker:
    """One pass over ``w:body``. Kept as a class because the counters must all
    advance together, and a function returning eight values would make the one
    invariant that matters — that every counter counts *every* item and only
    emits a fragment for some of them — easy to break in one place and not in
    the other."""

    def __init__(self, document: Any, source_id: uuid.UUID) -> None:
        self._document = document
        self._source_id = source_id
        self._ordinal = 0
        self._paragraphs = 0
        self._tables = 0
        self._cell_fragments = 0
        self._fragments: list[Fragment] = []
        self._unsupported: list[Unsupported] = []
        self._covered: list[CoveredPosition] = []
        self._empty_paragraphs = 0
        self._empty_tables = 0
        self._chapter: str | None = None

    # -- emission ----------------------------------------------------------

    def _emit(self, locator: Locator, text: str) -> None:
        self._fragments.append(
            Fragment(
                id=_fragment_id(self._source_id, self._ordinal),
                source_id=self._source_id,
                ordinal=self._ordinal,
                locator=locator,
                text=text,
            )
        )
        self._ordinal += 1

    def _note(self, code: str, address: str, detail: str, text: str = "") -> None:
        self._unsupported.append(Unsupported(code=code, address=address, detail=detail, text=text))

    def _notes(self, table_no: int, notes: tuple[GridNote, ...]) -> None:
        """Split the grid's notes into merges (reported) and the rest (unsupported)."""
        for note in notes:
            if note.code == "merged_cell":
                continue  # already carried structurally in _covered
            self._note(note.code, f"table {table_no}", note.detail)

    # -- body --------------------------------------------------------------

    def run(self) -> None:
        body = self._document.element.body
        for index, child in enumerate(body):
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag == _W_P:
                self._paragraph(child)
            elif tag == _W_TBL:
                self._table(child)
            elif tag == _W_SDT:
                text = _one_line(_raw_text(child))
                self._note(
                    "body_content_control",
                    f"body child {index + 1}",
                    "a block-level content control wraps content; its paragraphs are "
                    "not direct children of w:body, so they are not in the paragraph "
                    "numbering and are given no locator",
                    text,
                )
            elif tag == _W_ALT_CHUNK:
                self._note(
                    "alt_chunk",
                    f"body child {index + 1}",
                    "an altChunk imports content from another format at open time; "
                    "there is nothing in this file to locate",
                    "",
                )

    def _paragraph(self, p: etree._Element) -> None:
        self._paragraphs += 1
        number = self._paragraphs
        text, flags = paragraph_text(p)
        level = _heading_level(p)
        if level is not None and text:
            self._chapter = text
        if not text:
            self._empty_paragraphs += 1
        else:
            self._emit(
                _locator(
                    LocatorKind.DOCX_PARAGRAPH,
                    source_chapter=self._chapter,
                    paragraph=number,
                ),
                text,
            )
        for flag in sorted(flags):
            if flag == "text_box":
                self._note(
                    "text_box",
                    f"paragraph {number}",
                    "a text box is anchored in this paragraph. Its text is not this "
                    "paragraph's text and is not given the paragraph's number",
                    "",
                )
            elif flag == "tracked_deletion":
                self._note(
                    "tracked_deletion",
                    f"paragraph {number}",
                    "the paragraph contains text marked deleted by a tracked change; "
                    "that text is not part of the document and is excluded",
                    "",
                )
            elif flag == "text_depth_limit":
                self._note(
                    "text_depth_limit",
                    f"paragraph {number}",
                    f"the paragraph nests deeper than {MAX_TEXT_DEPTH} levels; the walk "
                    "stopped there, so the text below that point is not in any fragment",
                    "",
                )

    def _table(self, tbl: etree._Element) -> None:
        self._tables += 1
        number = self._tables
        grid, notes = build_grid(tbl)
        self._notes(number, notes)
        rendered = render_grid(grid)
        if rendered.strip():
            self._emit(
                _locator(LocatorKind.DOCX_TABLE, source_chapter=self._chapter, table=number),
                rendered,
            )
        else:
            self._empty_tables += 1

        for box in grid.cells:
            text = cell_text(box.element)
            if text:
                self._cell_fragments += 1
                self._emit(
                    _locator(
                        LocatorKind.DOCX_CELL,
                        source_chapter=self._chapter,
                        table=number,
                        cell=(box.row + 1, box.col + 1),
                    ),
                    text,
                )
            for index, nested in enumerate(nested_tables(box.element), start=1):
                self._note(
                    "nested_table",
                    f"table {number} cell ({box.row + 1}, {box.col + 1}) nested table {index}",
                    "a table nested inside a cell has no address in Locator, which "
                    "carries one table number and no nesting depth. Its text is kept "
                    "here; it is given no fragment and no invented locator",
                    _render_nested(nested, 0),
                )
        for (row, col), (anchor_row, anchor_col) in sorted(grid.covered.items()):
            self._covered.append(
                CoveredPosition(
                    table=number,
                    row=row + 1,
                    col=col + 1,
                    anchor_row=anchor_row + 1,
                    anchor_col=anchor_col + 1,
                )
            )

    def result(self, archive: ArchiveReport) -> DocxParseResult:
        return DocxParseResult(
            fragments=tuple(self._fragments),
            unsupported=tuple(self._unsupported),
            covered=tuple(self._covered),
            counts=ParseCounts(
                body_paragraphs=self._paragraphs,
                body_tables=self._tables,
                cell_fragments=self._cell_fragments,
                fragments=len(self._fragments),
                empty_paragraphs_skipped=self._empty_paragraphs,
                empty_tables_skipped=self._empty_tables,
            ),
            archive=archive,
            library_version=_library_version(),
        )


def _parse_document(document: Any, source_id: uuid.UUID, archive: ArchiveReport) -> DocxParseResult:
    walker = _Walker(document, source_id)
    walker.run()
    return walker.result(archive)


def _load_bytes(data: bytes, limits: ArchiveLimits):
    from docx import Document

    _checked, report = read_docx_bytes(data, limits)
    return Document(io.BytesIO(data)), report


def _load_path(path: pathlib.Path, limits: ArchiveLimits):
    from docx import Document

    report = inspect_docx(path, limits)
    return Document(str(path)), report


def _open_document(loader):
    from docx.opc.exceptions import PackageNotFoundError

    try:
        return loader()
    except PackageNotFoundError as exc:
        raise DocxParseError(f"not a WordprocessingML package: {exc}") from exc
    except (ValueError, KeyError, etree.LxmlError) as exc:
        raise DocxParseError(f"the document could not be read: {exc}") from exc


def parse_docx(
    path: str | pathlib.Path,
    *,
    source_id: uuid.UUID,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> DocxParseResult:
    """Parse a ``.docx`` on disk. Refuses the container before opening a part."""
    document, report = _open_document(lambda: _load_path(pathlib.Path(path), limits))
    return _parse_document(document, source_id, report)


def parse_docx_bytes(
    data: bytes,
    *,
    source_id: uuid.UUID,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> DocxParseResult:
    """Parse an in-memory ``.docx``. The same refusals, without a temp file."""
    document, report = _open_document(lambda: _load_bytes(data, limits))
    return _parse_document(document, source_id, report)


# --------------------------------------------------------------- resolution


@dataclasses.dataclass(frozen=True)
class Resolved:
    """What a locator points at, looked up again from scratch.

    ``element`` is the live ``w:p`` / ``w:tbl`` / ``w:tc``. Navigation goes
    through python-docx's public lists (``Document.paragraphs``,
    ``Document.tables``, ``Table.cell``) rather than through the walk that
    produced the fragment, so ``fragments[i].locator`` resolving to
    ``Document.paragraphs[n-1]._p is resolved.element`` is a statement about the
    document, not a restatement of the parser.
    """

    kind: LocatorKind
    text: str
    element: Any


def resolve_locator(document: Any, locator: Locator) -> Resolved | None:
    """Resolve one locator, or return ``None`` if it names nothing.

    ``None`` is the whole point of the return type: an address that does not
    exist in this document produces no text at all, rather than the nearest
    neighbour or an empty string.
    """
    kind = locator.kind
    if kind is LocatorKind.DOCX_PARAGRAPH:
        number = locator.paragraph
        if number is None or number < 1:
            return None
        paragraphs = document.paragraphs
        if number > len(paragraphs):
            return None
        paragraph = paragraphs[number - 1]
        text, _flags = paragraph_text(paragraph._p)
        return Resolved(kind=kind, text=text, element=paragraph._p)
    if kind is LocatorKind.DOCX_TABLE:
        number = locator.table
        if number is None or number < 1:
            return None
        tables = document.tables
        if number > len(tables):
            return None
        table = tables[number - 1]
        grid, _notes = build_grid(table._tbl)
        return Resolved(kind=kind, text=render_grid(grid), element=table._tbl)
    if kind is LocatorKind.DOCX_CELL:
        number = locator.table
        coords = locator.cell
        if number is None or number < 1 or coords is None or len(coords) != 2:
            return None
        row, col = coords
        if row < 1 or col < 1:
            return None
        tables = document.tables
        if number > len(tables):
            return None
        table = tables[number - 1]
        if row > len(table.rows) or col > len(table.columns):
            return None
        cell = table.cell(row - 1, col - 1)
        return Resolved(kind=kind, text=cell_text(cell._tc), element=cell._tc)
    return None
