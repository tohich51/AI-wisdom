"""C12A — a parsed fragment survives a real ``kb.fragment`` row, and a real read.

Everything here runs against a **real PostgreSQL 16.2** started by ``pgserver``,
on a dedicated database with every migration applied once per session. No test
double stands in for the server: the round trip is an INSERT, a COMMIT, a
second connection and a SELECT, and the refusals asserted here are the server's
own errors with their SQLSTATEs.

The question this file answers is narrower than "does the parser work": a
fragment is only provenance if the address survives storage. So each stored row
is read back, turned into a :class:`Locator` again, and resolved against the
document from scratch. Where the stored row cannot carry an address — and for
DOCX table and cell locators it cannot, because ``kb.fragment`` has no column
for one — the test proves the system degrades to ``None`` rather than to a
guess.

One known gap is *not* asserted here as a test: ``kb.fragment`` has no INSERT
policy, so the runtime role cannot write fragments at all. Asserting that a
refusal happens would be asserting that a defect exists, and it would start
failing the day someone fixes it. It is recorded in
``docs/handoff/results/C12A.json`` with the exact reproduction and the server's
own error, and the fragments below are written through the migration role.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from docx import Document
from docx_fixtures import DOCX_MEDIA_TYPE, fixture_path

from kb.access.policy import Principal, transaction_identity
from kb.catalog.parsers.docx_parser import parse_docx, resolve_locator
from kb.contracts.entities import Locator
from kb.contracts.enums import LocatorKind

pytest_plugins = ["docx_fixtures"]
pytestmark = pytest.mark.integration

INSERT_FRAGMENT = """
INSERT INTO kb.fragment
    (id, source_id, ordinal, locator_kind, file_page, printed_label, chapter,
     paragraph, snapshot_url, snapshot_at, text)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


@pytest.fixture
def parsed(world):
    """A real source row with a real file behind it, and its parsed fragments."""
    payload = fixture_path("basic.docx").read_bytes()
    library_id = world.library()
    source_id = world.source(library_id, payload)
    result = parse_docx(fixture_path("basic.docx"), source_id=source_id)
    return library_id, source_id, result


def store(world, source_id, fragment) -> None:
    """One fragment, written through every column the table actually has."""
    locator = fragment.locator
    world.conn.execute(
        INSERT_FRAGMENT,
        (
            fragment.id,
            source_id,
            fragment.ordinal,
            locator.kind.value,
            locator.file_page,
            locator.printed_label,
            locator.chapter,
            locator.paragraph,
            None if locator.snapshot_url is None else str(locator.snapshot_url),
            locator.snapshot_at,
            fragment.text,
        ),
    )


def locator_from_row(row) -> Locator:
    """Rebuild a Locator from exactly what the database gave back."""
    kind = LocatorKind(row[0])
    return Locator(
        kind=kind,
        file_page=row[1],
        printed_label=row[2],
        chapter=row[3],
        paragraph=row[4],
        snapshot_url=row[5],
        snapshot_at=row[6],
    )


def read_rows(conn, source_id):
    return list(
        conn.execute(
            "SELECT locator_kind, file_page, printed_label, chapter, paragraph, "
            "snapshot_url, snapshot_at, text FROM kb.fragment "
            "WHERE source_id = %s ORDER BY ordinal",
            (source_id,),
        ).fetchall()
    )


# ============================================================ the round trip


def test_a_parsed_fragment_survives_a_real_row_and_resolves_back_to_its_text(world, parsed):
    """Parse → INSERT → COMMIT → read back → resolve against the file."""
    _library, source_id, result = parsed
    for fragment in result.by_kind(LocatorKind.DOCX_PARAGRAPH):
        store(world, source_id, fragment)

    rows = read_rows(world.conn, source_id)
    assert len(rows) == len(result.by_kind(LocatorKind.DOCX_PARAGRAPH))

    document = Document(str(fixture_path("basic.docx")))
    for row, fragment in zip(rows, result.by_kind(LocatorKind.DOCX_PARAGRAPH), strict=True):
        assert row[0] == "docx_paragraph"
        assert row[7] == fragment.text
        rebuilt = locator_from_row(row)
        assert rebuilt == fragment.locator
        resolved = resolve_locator(document, rebuilt)
        assert resolved is not None
        assert resolved.text == fragment.text
        assert document.paragraphs[rebuilt.paragraph - 1]._p is resolved.element


def test_every_stored_row_records_no_page_number(world, parsed):
    """``missing page is null`` is enforced twice: by the parser and by the DDL."""
    _library, source_id, result = parsed
    for fragment in result.fragments:
        store(world, source_id, fragment)
    rows = read_rows(world.conn, source_id)
    assert rows
    assert all(row[1] is None for row in rows), "no DOCX row may carry a file_page"
    assert all(row[2] is None for row in rows), "no DOCX row may carry a printed label"


def test_the_server_refuses_a_docx_row_that_claims_a_page_number(world, parsed):
    """The CHECK constraint, with the server's own words, not an assertion about them.

    ``kb.fragment`` carries
    ``(locator_kind = 'pdf_file_page') = (file_page IS NOT NULL)``, so a row that
    is a DOCX paragraph *and* has a page is refused by PostgreSQL. This is the
    half of "pages are never invented" that lives in the schema rather than in
    the parser, and it is checked here against the running server.
    """
    _library, source_id, _result = parsed
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        world.conn.execute(
            INSERT_FRAGMENT,
            (
                uuid.uuid4(),
                source_id,
                900,
                "docx_paragraph",
                4,  # <- the invention
                None,
                "Brand voice",
                1,
                None,
                None,
                "this row should never exist",
            ),
        )
    assert 'check constraint "file_page_requires_kind"' in str(caught.value)
    assert "docx_paragraph" in str(caught.value)
    assert read_rows(world.conn, source_id) == [], "a refused row leaves nothing behind"


def test_a_paragraph_locator_stored_today_still_resolves_tomorrow(world, parsed):
    """The locator is a structural address, not a cache of the parse."""
    _library, source_id, result = parsed
    fragment = result.by_kind(LocatorKind.DOCX_PARAGRAPH)[1]
    store(world, source_id, fragment)
    stored = read_rows(world.conn, source_id)[0]
    rebuilt = locator_from_row(stored)
    document = Document(str(fixture_path("basic.docx")))
    resolved = resolve_locator(document, rebuilt)
    assert resolved is not None
    assert resolved.text == fragment.text
    assert rebuilt.paragraph == 2


# =============================================== the address that does not fit


def test_the_fragment_table_has_no_column_for_a_table_or_a_cell_address(world):
    """A stable statement about today's DDL, read from ``information_schema``.

    ``kb.fragment`` carries ``locator_kind, file_page, printed_label, chapter,
    paragraph, snapshot_url, snapshot_at``. There is nowhere to put ``table`` and
    nowhere to put ``cell``, so a DOCX cell address cannot be persisted as an
    address. That is a gap in the schema, not a licence to invent one: the
    contract change is proposed in the C12A result file for the single DDL owner
    and is not applied here, because migrations are outside this card's paths.

    This test is expected to fail the day those columns exist. That is what it is
    for — it is a marker on the schema, not a gate on correct behaviour.
    """
    columns = world.fragment_columns()
    assert set(columns) == {
        "id",
        "source_id",
        "ordinal",
        "locator_kind",
        "file_page",
        "printed_label",
        "chapter",
        "paragraph",
        "snapshot_url",
        "snapshot_at",
        "text",
    }
    assert "table" not in columns
    assert "cell" not in columns


def test_a_cell_fragment_stored_without_its_address_degrades_to_nothing(world, parsed):
    """The half of the contract that must hold even when storage is lossy.

    A cell fragment written with only the columns that exist comes back as a
    ``docx_cell`` row whose locator names no table and no cell. Resolving it
    yields ``None`` — the honest answer — and the parser's own address, which
    still points at the cell, is unaffected. A system that quietly substituted
    paragraph 1 here would be manufacturing provenance.
    """
    _library, source_id, result = parsed
    cell_fragment = next(
        f for f in result.by_kind(LocatorKind.DOCX_CELL) if f.locator.cell == (2, 3)
    )
    world.conn.execute(
        INSERT_FRAGMENT,
        (
            cell_fragment.id,
            source_id,
            500,
            "docx_cell",
            None,
            None,
            cell_fragment.locator.chapter,
            None,  # a cell has no paragraph number
            None,
            None,
            cell_fragment.text,
        ),
    )
    row = read_rows(world.conn, source_id)[0]
    rebuilt = locator_from_row(row)
    assert rebuilt.kind is LocatorKind.DOCX_CELL
    assert rebuilt.table is None and rebuilt.cell is None

    document = Document(str(fixture_path("basic.docx")))
    assert resolve_locator(document, rebuilt) is None

    intact = cell_fragment.locator
    resolved = resolve_locator(document, intact)
    assert resolved is not None
    assert resolved.text == "r2c3"
    assert document.tables[0].cell(1, 2)._tc is resolved.element


# ============================================================ the real read path


def test_a_reader_with_a_grant_sees_the_fragments_and_a_stranger_sees_none(world, kb_app, parsed):
    """The fragments are not unreachable: the real RLS read policy returns them.

    The pool is connected as ``kb_app``, which has no bypass, so this exercises
    ``fragment_read`` rather than the superuser's unconditional visibility.
    """
    library_id, source_id, result = parsed
    for fragment in result.fragments:
        store(world, source_id, fragment)
    reader, stranger = uuid.uuid4(), uuid.uuid4()
    world.grant(library_id, reader, "reader")

    def visible(principal_id: uuid.UUID) -> int:
        with (
            kb_app.connection() as conn,
            transaction_identity(conn, Principal(principal_id, uuid.uuid4())),
        ):
            rows = conn.execute(
                "SELECT count(*) FROM kb.fragment WHERE source_id = %s", (source_id,)
            ).fetchone()
        return int(rows[0])

    assert visible(reader) == len(result.fragments)
    assert visible(stranger) == 0


def test_the_stored_text_is_what_the_document_actually_says(world, parsed):
    """Not a summary, not a re-generation: the exact cell text."""
    _library, source_id, result = parsed
    cell = next(f for f in result.by_kind(LocatorKind.DOCX_CELL) if f.locator.cell == (1, 2))
    store(world, source_id, cell)
    row = read_rows(world.conn, source_id)[0]
    assert row[7] == "r1c2"
    assert row[0] == "docx_cell"
    assert row[3] == "Brand voice"


def test_the_source_row_says_it_is_a_docx_and_not_something_else(world, parsed):
    _library, source_id, _result = parsed
    media_type = world.conn.execute(
        "SELECT media_type FROM kb.source WHERE id = %s", (source_id,)
    ).fetchone()[0]
    assert media_type == DOCX_MEDIA_TYPE
