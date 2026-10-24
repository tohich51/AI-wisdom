"""Grid geometry of a WordprocessingML table: spans, merges, nesting.

This module knows nothing about text. It answers one question per table — *which
grid positions are real, independent cells, and which are a name for a cell that
already appeared elsewhere* — and it answers it by reading the XML, because the
convenient high-level API answers it by returning the same object twice and
calling that two cells.

The trap, in python-docx 1.2.0 terms: ``row.cells`` returns one ``_Cell`` per
**grid column**, and for a horizontally merged cell it returns *the same*
``_Cell`` object at every column the cell spans; for a vertically merged cell it
returns the anchor cell from an earlier row. Enumerating ``row.cells`` and
numbering what comes out therefore manufactures fragments at coordinates where
no separate cell exists, several of them carrying identical text. The output
would look like a faithful reading of a merged table and would be wrong.

The rule this module applies instead:

* One :class:`CellBox` per **origin** — one per ``w:tc`` that actually begins a
  cell. Its address is the top-left *grid* position of that element.
* Every other grid position is recorded in :attr:`TableGrid.covered`, mapped to
  the origin that owns it. Covered positions are addressable in the file and
  are not addressable as distinct cells, and the two facts are kept apart.
* A ``w:tbl`` inside a ``w:tc`` is reported, not numbered. See
  :class:`GridNote` code ``nested_table``.

Nothing here invents a coordinate. If the XML does not say where a cell is,
this module returns no cell for it.
"""

from __future__ import annotations

import dataclasses

from docx.oxml.ns import qn
from lxml.etree import _Element as XmlElement

__all__ = [
    "CellBox",
    "GridNote",
    "TableGrid",
    "build_grid",
    "nested_tables",
    "own_paragraphs",
    "table_rows",
]

Position = tuple[int, int]


@dataclasses.dataclass(frozen=True)
class CellBox:
    """One real cell, addressed at the top-left grid position it occupies.

    ``row`` and ``col`` are 0-based *grid* coordinates. The parser turns them
    into the 1-based ``Locator.cell`` the contract requires. ``element`` is the
    ``w:tc`` itself, so a caller can hold the identity and compare it later.
    """

    row: int
    col: int
    row_span: int
    col_span: int
    element: XmlElement

    @property
    def position(self) -> Position:
        return (self.row, self.col)


@dataclasses.dataclass(frozen=True)
class GridNote:
    """Something in this table that this parser declines to give a locator to.

    ``code`` is stable and machine-readable; ``detail`` is for a human. The
    parser prefixes ``detail`` with the table's own address, so a note read on
    its own never appears to be about the whole document.
    """

    code: str
    detail: str


@dataclasses.dataclass(frozen=True)
class TableGrid:
    """The resolved geometry of one ``w:tbl``."""

    declared_columns: int
    observed_columns: int
    row_count: int
    cells: tuple[CellBox, ...]
    covered: dict[Position, Position]

    @property
    def columns(self) -> int:
        return max(self.declared_columns, self.observed_columns)

    def box_at(self, row: int, col: int) -> CellBox | None:
        """The origin cell owning grid position (row, col), or ``None``."""
        anchor = self.covered.get((row, col), (row, col))
        for box in self.cells:
            if box.position == anchor:
                return box
        return None

    def is_covered(self, row: int, col: int) -> bool:
        return (row, col) in self.covered


def _tc_pr(tc: XmlElement) -> XmlElement | None:
    return tc.find(qn("w:tcPr"))


def grid_span(tc: XmlElement) -> int:
    """``w:gridSpan``, defaulting to 1. A nonsense value is 1, and says so.

    Treating ``w:gridSpan w:val="0"`` as 0 would shift every later column in the
    row and silently renumber cells that are perfectly well formed, so an
    out-of-range value is clamped rather than obeyed.
    """
    tcPr = _tc_pr(tc)
    if tcPr is None:
        return 1
    node = tcPr.find(qn("w:gridSpan"))
    if node is None:
        return 1
    raw = node.get(qn("w:val"))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


def vmerge(tc: XmlElement) -> str:
    """``"restart"``, ``"continue"`` or ``""`` (no vertical merge on this cell).

    ``<w:vMerge/>`` with no ``w:val`` means *continue*; only the explicit
    ``w:val="restart"`` opens a merge. Getting that backwards is how a parser
    ends up treating a merged column as a full stack of independent cells.
    """
    tcPr = _tc_pr(tc)
    if tcPr is None:
        return ""
    node = tcPr.find(qn("w:vMerge"))
    if node is None:
        return ""
    return "restart" if node.get(qn("w:val")) == "restart" else "continue"


def table_rows(tbl: XmlElement) -> list[XmlElement]:
    return [tr for tr in tbl if tr.tag == qn("w:tr")]


def own_paragraphs(tc: XmlElement) -> list[XmlElement]:
    """The ``w:p`` children of a cell — and only those.

    Paragraphs inside a nested table belong to that nested table, and paragraphs
    inside a drawing or a text box are not this cell's text. Both are excluded
    here and both are reported by the parser rather than quietly merged in.
    """
    return [child for child in tc if child.tag == qn("w:p")]


def nested_tables(tc: XmlElement) -> list[XmlElement]:
    return [child for child in tc if child.tag == qn("w:tbl")]


def declared_columns(tbl: XmlElement) -> int:
    grid = tbl.find(qn("w:tblGrid"))
    if grid is None:
        return 0
    return sum(1 for col in grid if col.tag == qn("w:gridCol"))


def build_grid(tbl: XmlElement) -> tuple[TableGrid, tuple[GridNote, ...]]:
    """Resolve one ``w:tbl`` into origins, covered positions and notes."""
    notes: list[GridNote] = []
    cells: list[CellBox] = []
    covered: dict[Position, Position] = {}
    # column -> (anchor row, anchor col) for a vertical merge still open
    open_merge: dict[int, Position] = {}
    observed = 0

    for row_index, tr in enumerate(table_rows(tbl)):
        col = 0
        for tc in tr:
            if tc.tag != qn("w:tc"):
                continue
            span = grid_span(tc)
            kind = vmerge(tc)
            if kind == "continue" and col in open_merge:
                anchor = open_merge[col]
            elif kind == "continue":
                # A continuation with no restart above it. The file is
                # inconsistent; pretending it opens a merge would attribute this
                # cell to a row it does not belong to.
                notes.append(
                    GridNote(
                        "orphan_vmerge",
                        f"a w:vMerge continuation at grid column {col + 1} has no "
                        f"restart above it; treated as the start of its own cell",
                    )
                )
                anchor = (row_index, col)
            else:
                for c in range(col, col + span):
                    open_merge.pop(c, None)
                anchor = (row_index, col)
                if kind == "restart":
                    for c in range(col, col + span):
                        open_merge[c] = anchor

            for offset in range(span):
                position = (row_index, col + offset)
                if position == anchor:
                    continue
                covered[position] = anchor
                notes.append(
                    GridNote(
                        "merged_cell",
                        f"grid position (row {row_index + 1}, column {col + offset + 1}) "
                        f"is covered by the cell anchored at (row {anchor[0] + 1}, "
                        f"column {anchor[1] + 1}) and is not a cell of its own",
                    )
                )
            cells.append(
                CellBox(row=anchor[0], col=anchor[1], row_span=1, col_span=span, element=tc)
            )
            col += span
        observed = max(observed, col)
        for c in [c for c in open_merge if c >= col]:
            del open_merge[c]

    # Nested tables are deliberately not touched here: this module has no
    # numbering to put one in, and the parser reports each with its address.
    declared = declared_columns(tbl)
    if declared and declared != observed:
        notes.append(
            GridNote(
                "grid_width_mismatch",
                f"w:tblGrid declares {declared} columns but the rows use {observed}",
            )
        )

    resolved = tuple(_with_row_spans(cells, covered))
    return (
        TableGrid(
            declared_columns=declared,
            observed_columns=observed,
            row_count=len(table_rows(tbl)),
            cells=resolved,
            covered=covered,
        ),
        tuple(notes),
    )


def _with_row_spans(cells: list[CellBox], covered: dict[Position, Position]) -> list[CellBox]:
    """Attach the vertical extent a merge actually has.

    A vertical merge in the file says only "this position continues something
    above". How far it reaches is decided by how many rows actually continue, so
    the number is counted from the covered positions rather than assumed.
    """
    heights: dict[Position, int] = {}
    for (row, _col), anchor in covered.items():
        heights[anchor] = max(heights.get(anchor, 1), row - anchor[0] + 1)
    return [
        CellBox(
            row=box.row,
            col=box.col,
            row_span=heights.get(box.position, 1),
            col_span=box.col_span,
            element=box.element,
        )
        for box in cells
    ]


def _own_cells(tbl: XmlElement) -> list[XmlElement]:
    """The ``w:tc`` elements of this table's own rows, and no others."""
    return [tc for tr in table_rows(tbl) for tc in tr if tc.tag == qn("w:tc")]
