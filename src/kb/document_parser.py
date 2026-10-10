"""Bounded, inert document parsing with original locations and no OCR or network."""

from pathlib import Path
from typing import Any, cast
from zipfile import ZipFile

from bs4 import BeautifulSoup
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from ebooklib import epub
from pypdf import PdfReader
from pypdf.generic import DictionaryObject


class ParseRejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NeedsOCR(ParseRejected):
    def __init__(self):
        super().__init__("needs_ocr")


def _html(text):
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    heading = soup.find(["h1", "h2", "h3"])
    return soup.get_text("\n", strip=True), heading.get_text(" ", strip=True) if heading else None


def parse_document(path: Path, media_type: str):
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ParseRejected("file_too_large")
    rows: list[dict[str, Any]] = []
    total = 0

    def add(text, kind="none", **locator):
        nonlocal total
        total += len(text)
        if total > 2_000_000:
            raise ParseRejected("text_too_large")
        if not text.strip():
            return
        for offset in range(0, len(text), 12000):
            rows.append(
                {
                    "ordinal": len(rows),
                    "text": text[offset : offset + 12000],
                    "locator_kind": kind,
                    "file_page": None,
                    "printed_label": None,
                    "chapter": None,
                    "paragraph": None,
                    "snapshot_url": None,
                    "snapshot_at": None,
                    **locator,
                }
            )

    try:
        with path.open("rb") as stream:
            magic = stream.read(8)
        if magic.startswith(b"%PDF"):
            pdf = PdfReader(path)
            if pdf.is_encrypted:
                raise ParseRejected("encrypted_document")
            if len(pdf.pages) > 1000:
                raise ParseRejected("too_many_pages")
            actual_labels = "/PageLabels" in cast(DictionaryObject, pdf.trailer["/Root"])
            labels = pdf.page_labels if actual_labels else []
            for number, page in enumerate(pdf.pages):
                add(
                    page.extract_text() or "",
                    "pdf_file_page",
                    file_page=number + 1,
                    printed_label=labels[number] if actual_labels else None,
                )
            if not rows:
                raise NeedsOCR()
        elif magic.startswith(b"PK"):
            with ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(e.file_size for e in entries) > 200 * 1024 * 1024:
                    raise ParseRejected("archive_too_large")
                for entry in entries:
                    if (
                        entry.flag_bits & 1
                        or entry.filename.startswith(("/", "\\"))
                        or ".." in entry.filename.replace("\\", "/").split("/")
                    ):
                        raise ParseRejected("invalid_archive")
                names = set(archive.namelist())
            if "word/document.xml" in names:
                document = Document(str(path))
                pindex, tindex = 0, 0
                for node in document.element.body.iterchildren():
                    if node.tag.endswith("}p"):
                        pindex += 1
                        add(Paragraph(node, document).text, "docx_paragraph", paragraph=pindex)
                    elif node.tag.endswith("}tbl"):
                        tindex += 1
                        for rindex, row in enumerate(Table(node, document).rows, start=1):
                            for cindex, cell in enumerate(row.cells, start=1):
                                add(
                                    cell.text,
                                    "docx_cell",
                                    table_index=tindex,
                                    cell_row=rindex,
                                    cell_column=cindex,
                                )
            elif "META-INF/container.xml" in names:
                book = epub.read_epub(str(path), options={"ignore_ncx": True})
                for ident, _ in book.spine:
                    item = book.get_item_with_id(ident)
                    if item:
                        text, chapter = _html(item.get_content())
                        for number, paragraph in enumerate(text.split("\n"), start=1):
                            add(paragraph, "epub_paragraph", chapter=chapter, paragraph=number)
            else:
                raise ParseRejected("unsupported_type")
        else:
            raw = path.read_bytes()
            if b"\0" in raw:
                raise ParseRejected("unsupported_type")
            text = raw.decode("utf-8-sig", errors="replace")
            html = media_type == "text/html" or text.lstrip().lower().startswith(
                ("<!doctype html", "<html")
            )
            chapter = None
            if html:
                text, chapter = _html(text)
            for number, paragraph in enumerate(text.split("\n"), start=1):
                add(
                    paragraph,
                    "html_snapshot" if html else "none",
                    chapter=chapter,
                    paragraph=number,
                )
        if not rows:
            raise ParseRejected("empty_document")
        return rows
    except ParseRejected:
        raise
    except Exception:
        raise ParseRejected("invalid_document") from None
