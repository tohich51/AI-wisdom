"""C11 — PDF fragments against a real PostgreSQL 16.2 (no mocks).

What this file is for: the parser produces a ``Fragment`` with two separate
locator columns, and the schema has CHECK constraints that exist precisely to
stop somebody flattening them. A parser test alone cannot show that the
database agrees; these tests put the real rows through the real server and
read them back, and they make the server's own refusals visible.

**A dedicated database.** ``migrations/0002_rls.sql`` creates policies and
``0001`` creates triggers, and neither is idempotent, so the schema is applied
ONCE per session into ``kb_c11_pdf``. One server, several cards, no ordering
dependency between them.

**Seeding happens as the migration role.** ``kb.fragment`` has RLS enabled and
forced with a SELECT policy and no INSERT policy (see
``test_there_is_no_fragment_write_policy_so_nobody_can_persist_a_fragment_yet``),
so arranging rows here uses the owner connection the way C10's test world
does. Every assertion about *what a caller may see* still goes through
``SET ROLE kb_app`` with ``app.principal`` set.
"""

from __future__ import annotations

import pathlib
import uuid
from typing import Any
from uuid import UUID

import pdf_fixtures as fx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from kb.catalog.parsers.pdf import fragment_row, parse_pdf

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
C11_PDF_DB = "kb_c11_pdf"

READER = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
CONTRIBUTOR = uuid.UUID("00000000-0000-4000-8000-0000000000d2")
STRANGER = uuid.UUID("00000000-0000-4000-8000-0000000000d3")


class SqlRunner:
    """One statement, one fresh connection, one chosen role and principal.

    Opening a connection per statement is what makes the identity assertions
    mean anything: nothing survives from the previous test, and a refusal comes
    back with the server's own SQLSTATE and message rather than as psql output.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def __call__(
        self,
        statement: str,
        *,
        role: str | None = None,
        principal: UUID | None = None,
        params: tuple | None = None,
    ) -> tuple[int, str]:
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                if role is not None:
                    conn.execute("SET ROLE " + sql.Identifier(role).as_string(None))  # type: ignore[arg-type]
                if principal is not None:
                    conn.execute("SELECT set_config('app.principal', %s, false)", (str(principal),))
                cur = conn.execute(statement, params)
                if cur.description is None:
                    return 0, ""
                return 0, "\n".join(
                    "\t".join("" if v is None else str(v) for v in row) for row in cur.fetchall()
                )
        except psycopg.Error as exc:
            return 1, f"[{exc.sqlstate}] {exc}"


@pytest.fixture(scope="session")
def c11_pdf_dsn(pg_server) -> str:
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (C11_PDF_DB,)
        ).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C11_PDF_DB)))
    return make_conninfo(base, dbname=C11_PDF_DB)


@pytest.fixture(scope="session")
def schema(c11_pdf_dsn: str) -> bool:
    """Apply 0001 → 0005 ONCE per session. Structure only, no content."""
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c11_pdf_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture(scope="session")
def run_sql(schema, c11_pdf_dsn: str) -> SqlRunner:
    return SqlRunner(c11_pdf_dsn)


@pytest.fixture
def world(schema, c11_pdf_dsn: str):
    """Arranges a library and a source, as the migration role.

    Everything it creates is scoped to this test's own library, so no test can
    see another's rows even though one database is shared by the session.
    """
    with psycopg.connect(c11_pdf_dsn, autocommit=True) as conn:
        org = uuid.uuid4()
        lib = uuid.uuid4()
        conn.execute(
            "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)", (org, f"c11-{org.hex[:8]}")
        )
        conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind) "
            "VALUES (%s, %s, %s, 'reference')",
            (lib, org, f"c11-lib-{lib.hex[:8]}"),
        )
        for who in (READER, CONTRIBUTOR):
            conn.execute(
                "INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES (%s, %s, %s)",
                (lib, who, "reader" if who is READER else "contributor"),
            )

        def source(media_type: str = "application/pdf", processing: str = "done") -> UUID:
            src = uuid.uuid4()
            conn.execute(
                "INSERT INTO kb.source (id, library_id, title, media_type, submitted_by, "
                "object_key, content_hash, processing) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    src,
                    lib,
                    "c11 synthetic source",
                    media_type,
                    CONTRIBUTOR,
                    f"c11/{src.hex}",
                    "0" * 64,
                    processing,
                ),
            )
            return src

        def insert_fragments(source_id: UUID, rows: list[dict[str, Any]]) -> tuple[int, str]:
            for row in rows:
                row = {**row, "source_id": source_id}
                conn.execute(
                    "INSERT INTO kb.fragment (id, source_id, ordinal, locator_kind, file_page, "
                    "printed_label, chapter, paragraph, text) "
                    "VALUES (%(id)s, %(source_id)s, %(ordinal)s, %(locator_kind)s, %(file_page)s, "
                    "%(printed_label)s, %(chapter)s, %(paragraph)s, %(text)s)",
                    row,
                )
            return 0, ""

        yield conn, source, insert_fragments


def _rows(conn, source_id: UUID) -> list[tuple]:
    return list(
        conn.execute(
            "SELECT ordinal, locator_kind, file_page, printed_label, chapter, paragraph, text "
            "FROM kb.fragment WHERE source_id = %s ORDER BY ordinal",
            (source_id,),
        ).fetchall()
    )


# ------------------------------------------------------- the two columns, stored


def test_every_parsed_pdf_fragment_lands_in_the_table_exactly_as_parsed(world):
    conn, source, insert = world
    src = source()
    result = parse_pdf(fx.text_book_pdf(), source_id=src)

    insert(src, [fragment_row(f) for f in result.fragments])

    assert _rows(conn, src) == [
        (
            0,
            "pdf_file_page",
            1,
            "i",
            None,
            None,
            "Title page of a synthetic book\nno author is written here",
        ),
        (1, "pdf_file_page", 2, "ii", None, None, "Contents\nIntroduction .... 1"),
        (
            2,
            "pdf_file_page",
            3,
            "1",
            None,
            None,
            "Chapter one begins on the page printed as 1\nsecond line",
        ),
        (3, "pdf_file_page", 4, "2", None, None, "Chapter one continues on the page printed as 2"),
    ]


def test_the_two_page_columns_are_stored_side_by_side_and_do_not_collapse(world, run_sql):
    """The invariant, in SQL rather than in Python.

    ``file_page::text = printed_label`` is false for every row, which is the
    whole point: in this book the printed label and the file page disagree, and
    the storage keeps both.
    """
    _conn, source, insert = world
    src = source()
    result = parse_pdf(fx.text_book_pdf(), source_id=src)
    insert(src, [fragment_row(f) for f in result.fragments])

    rc, out = run_sql(
        "SELECT file_page, printed_label, file_page::text = printed_label AS collapsed "
        "FROM kb.fragment WHERE source_id = %s ORDER BY ordinal",
        params=(str(src),),
    )
    assert rc == 0, out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 4
    assert all(line.endswith("\tFalse") for line in lines), out
    assert "3\t1\tFalse" in out


# --------------------------------------------------------- the server's refusals


def test_the_database_refuses_a_printed_label_that_carries_a_file_page(world, run_sql):
    """The CHECK is not worked around anywhere in this card — it is tested.

    ``printed_label_is_not_a_page`` says a ``pdf_printed_label`` row must have a
    NULL file_page. The only honest thing to do with a page that has both a file
    position and a printed label is what the parser does: keep
    ``pdf_file_page`` and put the label in the label column.

    The server names ``file_page_requires_kind`` rather than
    ``printed_label_is_not_a_page``, and that is not an accident of wording:
    every row that violates the second constraint also violates the first
    (which is defined earlier), so the second one can never be the constraint
    the server reports. The guarantee is real either way — the row is refused —
    and the redundancy is reported to the DDL owner.
    """
    conn, source, _insert = world
    src = source()
    rc, out = run_sql(
        "INSERT INTO kb.fragment (source_id, ordinal, locator_kind, file_page, "
        "printed_label, text) VALUES (%s, 0, 'pdf_printed_label', 3, '1', 'x')",
        params=(str(src),),
    )
    assert rc == 1
    assert "check constraint" in out, out
    assert "file_page_requires_kind" in out, out
    assert _rows(conn, src) == []


def test_a_pdf_printed_label_row_with_no_file_page_is_the_shape_the_table_wants(world, run_sql):
    """The legal counterpart of the refusal above, stored for real.

    A printed label on its own is a legitimate row: the label column holds the
    label and the file page column stays empty. The parser chooses the other
    shape for a page it actually read a position for, and this test is what
    makes the difference between "forbidden" and "chose not to" explicit.
    """
    conn, source, _insert = world
    src = source()
    rc, out = run_sql(
        "INSERT INTO kb.fragment (source_id, ordinal, locator_kind, printed_label, text) "
        "VALUES (%s, 0, 'pdf_printed_label', 'xii', 'front matter')",
        params=(str(src),),
    )
    assert rc == 0, out
    assert _rows(conn, src) == [(0, "pdf_printed_label", None, "xii", None, None, "front matter")]


def test_the_database_refuses_a_file_page_under_a_non_pdf_kind(world, run_sql):
    """``file_page_requires_kind``: file_page exists if and only if the kind is
    ``pdf_file_page``. An EPUB fragment with a page number is refused, which is
    what keeps an invented EPUB page out of the table."""
    conn, source, _insert = world
    src = source()
    rc, out = run_sql(
        "INSERT INTO kb.fragment (source_id, ordinal, locator_kind, file_page, chapter, "
        "paragraph, text) VALUES (%s, 0, 'epub_chapter', 12, 'One', 1, 'x')",
        params=(str(src),),
    )
    assert rc == 1
    assert "file_page_requires_kind" in out, out
    assert _rows(conn, src) == []


def test_the_contract_refuses_the_same_combination_before_the_database_is_reached():
    from pydantic import ValidationError

    from kb.contracts.entities import Locator
    from kb.contracts.enums import LocatorKind

    with pytest.raises(ValidationError, match="printed label is not a file page"):
        Locator(kind=LocatorKind.PDF_PRINTED_LABEL, file_page=3, printed_label="1")
    with pytest.raises(ValidationError, match="pdf_file_page requires file_page"):
        Locator(kind=LocatorKind.PDF_FILE_PAGE, printed_label="1")


def test_two_fragments_of_one_source_cannot_share_an_ordinal(world, run_sql):
    _conn, source, insert = world
    src = source()
    result = parse_pdf(fx.text_book_pdf(), source_id=src)
    rows = [fragment_row(f) for f in result.fragments]
    insert(src, rows[:1])
    rc, out = run_sql(
        "INSERT INTO kb.fragment (source_id, ordinal, locator_kind, file_page, text) "
        "VALUES (%s, 0, 'pdf_file_page', 9, 'x')",
        params=(str(src),),
    )
    assert rc == 1
    assert "source_id, ordinal" in out, out


# ------------------------------------------------------------- the missing text


def test_a_page_less_scan_writes_no_fragment_rows_and_the_source_says_needs_ocr(world):
    """The card's central promise, through the database.

    ``scan.pdf`` has no text layer. The parse produces no fragments, so there
    is nothing to write, and the source is left in ``needs_ocr`` where a reader
    can see that the book has not been read rather than finding an empty shelf
    and no explanation.
    """
    conn, source, insert = world
    src = source(processing="needs_ocr")
    result = parse_pdf(fx.scan_pdf(), source_id=src)

    assert result.processing.value == "needs_ocr"
    insert(src, [fragment_row(f) for f in result.fragments])

    assert _rows(conn, src) == []
    assert conn.execute("SELECT processing FROM kb.source WHERE id = %s", (src,)).fetchone() == (
        "needs_ocr",
    )


def test_a_half_scanned_document_writes_rows_only_where_text_was_read(world):
    conn, source, insert = world
    src = source(processing="partial")
    data = fx.build_pdf(
        [
            fx.PdfPage(lines=["typed page one"]),
            fx.PdfPage(image=True),
            fx.PdfPage(lines=["typed page three"]),
        ]
    )
    result = parse_pdf(data, source_id=src)
    assert result.processing.value == "partial"

    insert(src, [fragment_row(f) for f in result.fragments])

    stored = _rows(conn, src)
    assert [(row[2], row[6]) for row in stored] == [(1, "typed page one"), (3, "typed page three")]
    # the page that could not be read has no row, invented or otherwise
    assert 2 not in [row[2] for row in stored]


# ------------------------------------------------------------------------- RLS


def test_a_reader_can_read_the_fragments_of_a_library_they_may_read(world, run_sql):
    _conn, source, insert = world
    src = source()
    result = parse_pdf(fx.text_book_pdf(), source_id=src)
    insert(src, [fragment_row(f) for f in result.fragments])

    rc, out = run_sql(
        "SELECT count(*) FROM kb.fragment WHERE source_id = %s",
        role="kb_app",
        principal=READER,
        params=(str(src),),
    )
    assert rc == 0, out
    assert out.strip() == "4", out


def test_a_stranger_reads_no_fragment_of_a_book_they_may_not_see(world, run_sql):
    _conn, source, insert = world
    src = source()
    result = parse_pdf(fx.text_book_pdf(), source_id=src)
    insert(src, [fragment_row(f) for f in result.fragments])

    rc, out = run_sql(
        "SELECT count(*) FROM kb.fragment WHERE source_id = %s",
        role="kb_app",
        principal=STRANGER,
        params=(str(src),),
    )
    assert rc == 0, out
    assert out.strip() == "0", out


def test_there_is_no_fragment_write_policy_so_nobody_can_persist_a_fragment_yet(world, run_sql):
    """A recorded gap, not a workaround.

    ``kb.fragment`` has RLS enabled and FORCED with exactly one policy,
    ``fragment_read`` (SELECT). There is no INSERT policy, and 0002's blanket
    ``GRANT ... INSERT ON ALL TABLES`` is then useless because FORCE RLS applies
    to the runtime role as well. So a contributor cannot write a fragment — the
    worker that will run this parser has nowhere to put its output.

    ``migrations/`` belongs to a single owner and is outside this card, so the
    missing policy is reported rather than added. This test is a detector: if
    the owner adds ``fragment_write``, this test fails and has to be rewritten
    as the positive assertion that a contributor's insert succeeds.
    """
    conn, source, insert = world
    src = source()
    result = parse_pdf(fx.text_book_pdf(), source_id=src)
    rows = [fragment_row(f) for f in result.fragments]

    insert(src, rows[:1])  # the migration role can; the runtime role cannot:
    rc, out = run_sql(
        "INSERT INTO kb.fragment (source_id, ordinal, locator_kind, file_page, text) "
        "VALUES (%s, 0, 'pdf_file_page', 9, 'x')",
        role="kb_app",
        principal=CONTRIBUTOR,
        params=(str(src),),
    )
    assert rc == 1
    assert "row-level security" in out.lower(), out
    assert "fragment" in out.lower(), out
    # the row the runtime role tried to write is not there
    assert [row[2] for row in _rows(conn, src)] == [1]
