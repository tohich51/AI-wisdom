"""C12A — a table address names a table, a cell address names one real cell.

This is where a DOCX parser is most tempted to lie, and this file is the
evidence that it does not. Two specific failures are ruled out:

* A **merged cell** is one ``w:tc``. Enumerating the grid by column sees the
  same object at every column it spans, and a parser that numbers what it sees
  produces several fragments at addresses where no separate cell exists, several
  of them carrying the same text. ``merged_cells.docx`` has a horizontal
  ``w:gridSpan`` merge and a vertical ``w:vMerge`` chain, and the assertion is
  not "the right number of fragments" but that the duplicates exist in the
  high-level API and the parser declines to copy them.

* A **nested table** has no address in ``Locator``, which carries one table
  number and no nesting depth. Inventing a second numbering would create
  addresses nothing else in the system could resolve. The inner table's text is
  preserved in the unsupported report and it gets no fragment.

The documents are real ``.docx`` packages from ``evals/fixtures/docx``, written
by python-docx 1.2.0 (MIT). Nothing here is a mock and nothing here is a real
document from anywhere.
"""

from __future__ import annotations

import uuid

import pytest
from docx import Document
from docx_fixtures import fixture_path

from kb.catalog.parsers.docx_parser import parse_docx, resolve_locator
from kb.contracts.enums import LocatorKind

pytest_plugins = ["docx_fixtures"]
pytestmark = pytest.mark.integration

SOURCE = uuid.uuid5(uuid.NAMESPACE_URL, "kb-c12a-source")


def parse(name: str):
    return parse_docx(fixture_path(name), source_id=SOURCE)


def reopened(name: str) -> Document:
    return Document(str(fixture_path(name)))


def cells(result, kind=LocatorKind.DOCX_CELL) -> dict[tuple[int, int], str]:
    return {f.locator.cell: f.text for f in result.by_kind(kind)}


# ================================================================== plain table


def test_a_plain_table_yields_one_table_fragment_and_one_fragment_per_real_cell():
    result = parse("basic.docx")
    tables = result.by_kind(LocatorKind.DOCX_TABLE)
    assert len(tables) == 1
    assert tables[0].locator.table == 1
    assert tables[0].locator.paragraph is None
    assert cells(result) == {
        (1, 1): "r1c1",
        (1, 2): "r1c2",
        (1, 3): "r1c3",
        (2, 1): "r2c1",
        (2, 2): "r2c2",
        (2, 3): "r2c3",
    }


def test_a_table_address_is_exactly_document_tables_n_minus_one():
    document = reopened("basic.docx")
    table = parse("basic.docx").by_kind(LocatorKind.DOCX_TABLE)[0]
    resolved = resolve_locator(document, table.locator)
    assert resolved is not None
    assert document.tables[table.locator.table - 1]._tbl is resolved.element
    assert resolved.text == table.text


def test_a_cell_address_is_exactly_that_table_that_cell():
    """Identity, through python-docx's own ``Table.cell``, for every address."""
    document = reopened("basic.docx")
    for fragment in parse("basic.docx").by_kind(LocatorKind.DOCX_CELL):
        row, col = fragment.locator.cell
        resolved = resolve_locator(document, fragment.locator)
        assert resolved is not None
        assert document.tables[0].cell(row - 1, col - 1)._tc is resolved.element
        assert resolved.text == fragment.text


def test_the_table_fragment_keeps_the_grid_shape_of_the_table():
    result = parse("basic.docx")
    table = result.by_kind(LocatorKind.DOCX_TABLE)[0]
    lines = table.text.splitlines()
    assert len(lines) == 2
    assert all(len(line.split(" | ")) == 3 for line in lines)
    assert lines[0] == "r1c1 | r1c2 | r1c3"


def test_a_table_and_its_cells_are_ordered_the_way_the_document_is():
    result = parse("basic.docx")
    order = [f.locator.kind.value for f in result.fragments]
    assert order == (
        ["docx_paragraph"] * 3 + ["docx_table"] + ["docx_cell"] * 6 + ["docx_paragraph"] * 2
    )


def test_a_cell_with_several_paragraphs_keeps_them_separated():
    import io

    from docx import Document as _Document

    document = _Document()
    table = document.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    cell.text = "first line"
    cell.add_paragraph("second line")
    buffer = io.BytesIO()
    document.save(buffer)
    result = parse_docx_bytes_helper(buffer.getvalue())
    assert cells(result) == {(1, 1): "first line\nsecond line"}


def parse_docx_bytes_helper(payload: bytes):
    return parse_docx(payload_path_helper(payload), source_id=SOURCE)


def payload_path_helper(payload: bytes):
    import pathlib
    import tempfile

    path = pathlib.Path(tempfile.mkdtemp(prefix="c12a-")) / "made.docx"
    path.write_bytes(payload)
    return path


# ================================================================ merged cells


def test_the_high_level_api_really_does_hand_back_one_cell_for_several_addresses():
    """The premise, proved with python-docx's own public list.

    Without this, "the parser emitted fewer cells than the grid" could just mean
    the parser is wrong. ``row.cells`` returns nine entries for a 3x3 table whose
    grid has seven independent cells, and two of those nine are the *same object*
    as two others.
    """
    table = reopened("merged_cells.docx").tables[0]
    grid = [cell._tc for row in table.rows for cell in row.cells]
    assert len(grid) == 9
    assert len({id(tc) for tc in grid}) == 7
    assert grid[2] is grid[1], "the horizontal merge repeats one cell"
    assert grid[6] is grid[3], "the vertical merge repeats the anchor cell"


def test_a_merged_cell_is_emitted_once_at_its_origin_and_never_at_a_covered_position():
    result = parse("merged_cells.docx")
    assert cells(result) == {
        (1, 1): "r1c1",
        (1, 2): "spans two columns",
        (2, 1): "merged down three rows",
        (2, 2): "r2c2",
        (2, 3): "r2c3",
        (3, 2): "r3c2",
        (3, 3): "r3c3",
    }
    assert (1, 3) not in cells(result), "a covered grid position is not a cell"
    assert (3, 1) not in cells(result), "a vMerge continuation is not a cell"


def test_every_covered_position_is_reported_with_the_address_that_owns_it():
    result = parse("merged_cells.docx")
    assert [(c.row, c.col, c.anchor_row, c.anchor_col) for c in result.covered] == [
        (1, 3, 1, 2),
        (3, 1, 2, 1),
    ]
    assert all(c.table == 1 for c in result.covered)


def test_a_covered_position_still_resolves_to_the_text_of_the_cell_that_owns_it():
    """The merge is a fact about the file, not a hole in the parser.

    Asked for (1, 3), python-docx hands back the merged cell — which is exactly
    why the parser did not mint a fragment there. The assertion ties the two
    observations together.
    """
    document = reopened("merged_cells.docx")
    from kb.contracts.entities import Locator

    covered_horizontal = Locator(kind=LocatorKind.DOCX_CELL, table=1, cell=(1, 3))
    resolved = resolve_locator(document, covered_horizontal)
    assert resolved is not None
    assert resolved.text == "spans two columns"
    origin = next(
        f
        for f in parse("merged_cells.docx").fragments
        if f.locator.kind is LocatorKind.DOCX_CELL and f.locator.cell == (1, 2)
    )
    assert resolved.element is document.tables[0].cell(0, 1)._tc
    assert resolved.text == origin.text


def test_the_merged_table_fragment_shows_the_merge_once_rather_than_three_times():
    table = parse("merged_cells.docx").by_kind(LocatorKind.DOCX_TABLE)[0]
    assert table.text.splitlines() == [
        "r1c1 | spans two columns | [merged]",
        "merged down three rows | r2c2 | r2c3",
        "[merged] | r3c2 | r3c3",
    ]


def test_a_merged_cell_fragment_resolves_back_to_its_own_element():
    document = reopened("merged_cells.docx")
    for fragment in parse("merged_cells.docx").by_kind(LocatorKind.DOCX_CELL):
        row, col = fragment.locator.cell
        resolved = resolve_locator(document, fragment.locator)
        assert resolved is not None
        assert document.tables[0].cell(row - 1, col - 1)._tc is resolved.element
        assert resolved.text == fragment.text


def test_a_vertically_merged_cell_does_not_repeat_its_own_text_in_the_table():
    """``merged down three rows`` appears once in the table fragment, not three times."""
    table = parse("merged_cells.docx").by_kind(LocatorKind.DOCX_TABLE)[0]
    assert table.text.count("merged down three rows") == 1


# =============================================================== nested tables


def test_a_nested_table_is_reported_with_its_text_and_gets_no_locator():
    result = parse("nested_table.docx")
    notes = [u for u in result.unsupported if u.code == "nested_table"]
    assert len(notes) == 1
    assert notes[0].address == "table 1 cell (2, 2) nested table 1"
    assert notes[0].text.splitlines() == [
        "inner r1c1 | inner r1c2",
        "inner r2c1 | inner r2c2",
    ]
    assert "not a fragment" in notes[0].detail or "given no fragment" in notes[0].detail


def test_no_fragment_anywhere_claims_the_nested_table_as_a_top_level_one():
    result = parse("nested_table.docx")
    assert len(result.by_kind(LocatorKind.DOCX_TABLE)) == 1
    assert result.by_kind(LocatorKind.DOCX_TABLE)[0].locator.table == 1
    assert all("inner r" not in f.text for f in result.fragments)


def test_the_owning_cell_fragment_holds_only_the_cells_own_paragraphs():
    result = parse("nested_table.docx")
    assert cells(result)[(2, 2)] == "and text after the nested table"
    assert "inner" not in cells(result)[(2, 2)]


def test_the_outer_table_fragment_says_a_cell_holds_a_nested_table():
    table = parse("nested_table.docx").by_kind(LocatorKind.DOCX_TABLE)[0]
    assert table.text.splitlines() == [
        "outer 1,1 | outer 1,2",
        "outer 2,1 | and text after the nested table [nested table]",
    ]


def test_the_outer_cell_address_still_names_that_cell_and_not_the_table_inside_it():
    document = reopened("nested_table.docx")
    fragment = next(
        f
        for f in parse("nested_table.docx").by_kind(LocatorKind.DOCX_CELL)
        if f.locator.cell == (2, 2)
    )
    resolved = resolve_locator(document, fragment.locator)
    assert resolved is not None
    assert document.tables[0].cell(1, 1)._tc is resolved.element
    assert resolved.text == fragment.text


def test_nothing_is_lost_between_the_fragments_and_the_report():
    """Every cell of the nested table is recoverable, just not as a fragment."""
    notes = [u for u in parse("nested_table.docx").unsupported if u.code == "nested_table"]
    for row in range(1, 3):
        for col in range(1, 3):
            assert f"inner r{row}c{col}" in notes[0].text


# =================================================== empty and edge-case tables


def test_an_empty_table_is_counted_but_produces_no_fragments(tmp_path):
    import io

    from docx import Document as _Document

    document = _Document()
    document.add_paragraph("before")
    document.add_table(rows=1, cols=1)
    document.add_paragraph("after")
    buffer = io.BytesIO()
    document.save(buffer)
    path = payload_path_helper(buffer.getvalue())
    result = parse_docx(path, source_id=SOURCE)
    assert result.counts.body_tables == 1
    assert result.counts.empty_tables_skipped == 1
    assert not result.by_kind(LocatorKind.DOCX_TABLE)
    numbers = [f.locator.paragraph for f in result.by_kind(LocatorKind.DOCX_PARAGRAPH)]
    assert numbers == [1, 2], "an empty table must not disturb the paragraph numbering"
