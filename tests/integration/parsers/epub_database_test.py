"""C11 — EPUB fragments against a real PostgreSQL 16.2 (no mocks).

The parser half of this card proves an EPUB never receives a page number. This
half proves the *database* would not accept one even if some later change tried
to put it there, and that the rows which are written carry the reading-order
locator the book actually declares.

**A dedicated database, applied once per session.** ``kb_c11_epub`` is separate
from ``kb_c11_pdf`` (see ``pdf_database_test.py``) because ``CREATE POLICY`` and
``CREATE TRIGGER`` are not idempotent and both files need the same schema.

**Why the harness is repeated here.** The card's allowed test paths are
``tests/integration/parsers/pdf*`` and ``tests/integration/parsers/epub*`` —
there is no neutral filename for a shared ``conftest.py`` or ``db_support.py``
in this directory, so the small runner and fixtures are written out again
rather than smuggled into a file named after one of the two formats. The
integrator is free to factor them out once a neutral path exists.
"""

from __future__ import annotations

import pathlib
import uuid
from typing import Any
from uuid import UUID

import epub_fixtures as fx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from kb.catalog.parsers.epub import fragment_row, parse_epub

pytestmark = pytest.mark.integration

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
C11_EPUB_DB = "kb_c11_epub"

READER = uuid.UUID("00000000-0000-4000-8000-0000000000e1")
STRANGER = uuid.UUID("00000000-0000-4000-8000-0000000000e2")


class SqlRunner:
    """One statement, one fresh connection, one chosen role and principal."""

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
def c11_epub_dsn(pg_server) -> str:
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (C11_EPUB_DB,)
        ).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C11_EPUB_DB)))
    return make_conninfo(base, dbname=C11_EPUB_DB)


@pytest.fixture(scope="session")
def schema(c11_epub_dsn: str) -> bool:
    """Apply the migrations ONCE per session. Structure only, no content."""
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c11_epub_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture(scope="session")
def run_sql(schema, c11_epub_dsn: str) -> SqlRunner:
    return SqlRunner(c11_epub_dsn)


@pytest.fixture
def world(schema, c11_epub_dsn: str):
    """A library, a grant and a source, arranged as the migration role.

    ``kb.fragment`` has no INSERT policy under RLS — see
    ``test_there_is_no_fragment_write_policy_so_nobody_can_persist_a_fragment_yet``
    in ``pdf_database_test.py`` — so arranging rows uses the owner connection.
    Assertions about what a caller may see still run as ``kb_app``.
    """
    with psycopg.connect(c11_epub_dsn, autocommit=True) as conn:
        org = uuid.uuid4()
        lib = uuid.uuid4()
        conn.execute(
            "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)", (org, f"c11e-{org.hex[:8]}")
        )
        conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind) "
            "VALUES (%s, %s, %s, 'reference')",
            (lib, org, f"c11e-lib-{lib.hex[:8]}"),
        )
        conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) "
            "VALUES (%s, %s, 'reader')",
            (lib, READER),
        )

        def source(processing: str = "done") -> UUID:
            src = uuid.uuid4()
            conn.execute(
                "INSERT INTO kb.source (id, library_id, title, media_type, submitted_by, "
                "object_key, content_hash, processing) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    src,
                    lib,
                    "c11 synthetic epub source",
                    "application/epub+zip",
                    READER,
                    f"c11e/{src.hex}",
                    "1" * 64,
                    processing,
                ),
            )
            return src

        def insert_fragments(source_id: UUID, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                conn.execute(
                    "INSERT INTO kb.fragment (id, source_id, ordinal, locator_kind, file_page, "
                    "printed_label, chapter, paragraph, text) "
                    "VALUES (%(id)s, %(source_id)s, %(ordinal)s, %(locator_kind)s, %(file_page)s, "
                    "%(printed_label)s, %(chapter)s, %(paragraph)s, %(text)s)",
                    {**row, "source_id": source_id},
                )

        yield conn, source, insert_fragments


def _rows(conn, source_id: UUID) -> list[tuple]:
    return list(
        conn.execute(
            "SELECT ordinal, locator_kind, file_page, printed_label, chapter, paragraph, text "
            "FROM kb.fragment WHERE source_id = %s ORDER BY ordinal",
            (source_id,),
        ).fetchall()
    )


# ------------------------------------------------------------------- round trip


def test_every_parsed_epub_fragment_lands_in_the_table_exactly_as_parsed(world):
    conn, source, insert = world
    src = source()
    result = parse_epub(fx.booklet_epub(), source_id=src)
    insert(src, [fragment_row(f) for f in result.fragments])

    stored = _rows(conn, src)
    assert len(stored) == 14
    assert [row[1] for row in stored] == ["epub_chapter"] * 14
    assert [(row[4], row[5]) for row in stored[:2]] == [("Cover", 1), ("Cover", 2)]
    assert [(row[4], row[5]) for row in stored[2:7]] == [("Chapter One", n) for n in range(1, 6)]
    assert stored[0][6] == "A Synthetic Booklet"
    assert stored[3][6] == "A paragraph that exists only in this synthetic book."


def test_no_epub_row_in_the_table_carries_a_page(world, run_sql):
    """Acceptance 2, at the storage layer rather than in the parser."""
    conn, source, insert = world
    src = source()
    result = parse_epub(fx.booklet_epub(), source_id=src)
    insert(src, [fragment_row(f) for f in result.fragments])

    rc, out = run_sql(
        "SELECT count(*) FROM kb.fragment WHERE source_id = %s AND "
        "(file_page IS NOT NULL OR printed_label IS NOT NULL)",
        params=(str(src),),
    )
    assert rc == 0, out
    assert out.strip() == "0", out
    assert all(row[2] is None and row[3] is None for row in _rows(conn, src))


def test_the_database_refuses_an_epub_row_that_someone_gave_a_page(world, run_sql):
    """The invention is refused at the boundary, not just avoided by the parser.

    The row below is a real parsed fragment with one thing changed: a page
    number copied onto it. ``file_page_requires_kind`` refuses it, so an EPUB
    page can only appear if the CHECK itself is dropped — which is the DDL
    owner's decision to make visibly, not a parser's.
    """
    conn, source, _insert = world
    src = source()
    rc, out = run_sql(
        "INSERT INTO kb.fragment (source_id, ordinal, locator_kind, file_page, chapter, "
        "paragraph, text) VALUES (%s, 0, 'epub_chapter', 12, 'Chapter One', 1, 'invented')",
        params=(str(src),),
    )
    assert rc == 1
    assert "file_page_requires_kind" in out, out
    assert _rows(conn, src) == []


def test_the_spine_position_has_no_column_so_the_row_cannot_carry_it(world, run_sql):
    """A known loss, recorded so it cannot be forgotten.

    ``kb.fragment`` has no ``spine`` column, so a reading-order position read
    from the OPF does not survive the write. The in-memory locator keeps it and
    ``text_at_locator`` uses it; the stored row does not have it. This test is a
    detector: when the DDL owner adds the column, this fails and becomes the
    positive assertion that the position round-trips.
    """
    conn, source, insert = world
    src = source()
    result = parse_epub(fx.booklet_epub(), source_id=src)
    in_memory = {f.ordinal: f.locator.spine for f in result.fragments}
    assert in_memory[0] == "1" and in_memory[2] == "2" and in_memory[7] == "3"

    insert(src, [fragment_row(f) for f in result.fragments])

    rc, out = run_sql(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'kb' AND table_name = 'fragment' AND column_name = 'spine'",
    )
    assert rc == 0, out
    assert out.strip() == "0", out  # the column does not exist
    # the stored rows are otherwise complete: the paragraph numbers restart per
    # spine document, so the reading order is still visible in what survived
    assert [row[5] for row in _rows(conn, src)] == [1, 2, 1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 6, 7]


# ------------------------------------------------------------- the missing text


def test_a_book_of_images_writes_no_fragment_rows_and_the_source_says_needs_ocr(world):
    conn, source, insert = world
    src = source(processing="needs_ocr")
    result = parse_epub(fx.scan_epub(), source_id=src)

    assert result.processing.value == "needs_ocr"
    insert(src, [fragment_row(f) for f in result.fragments])

    assert _rows(conn, src) == []
    assert conn.execute("SELECT processing FROM kb.source WHERE id = %s", (src,)).fetchone() == (
        "needs_ocr",
    )


# ------------------------------------------------------------------------- RLS


def test_a_reader_can_read_the_fragments_of_a_library_they_may_read(world, run_sql):
    _conn, source, insert = world
    src = source()
    result = parse_epub(fx.booklet_epub(), source_id=src)
    insert(src, [fragment_row(f) for f in result.fragments])

    rc, out = run_sql(
        "SELECT count(*) FROM kb.fragment WHERE source_id = %s",
        role="kb_app",
        principal=READER,
        params=(str(src),),
    )
    assert rc == 0, out
    assert out.strip() == "14", out


def test_a_stranger_reads_no_fragment_of_a_book_they_may_not_see(world, run_sql):
    _conn, source, insert = world
    src = source()
    result = parse_epub(fx.booklet_epub(), source_id=src)
    insert(src, [fragment_row(f) for f in result.fragments])

    rc, out = run_sql(
        "SELECT count(*) FROM kb.fragment WHERE source_id = %s",
        role="kb_app",
        principal=STRANGER,
        params=(str(src),),
    )
    assert rc == 0, out
    assert out.strip() == "0", out
