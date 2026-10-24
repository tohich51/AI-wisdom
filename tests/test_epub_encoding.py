"""Actual-container regressions: encoding refusal must precede persistence."""

import codecs
import io
import tempfile
import unittest
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

from ebooklib import epub

from kb.catalog.parsers.epub import parse_epub
from kb.contracts.enums import ProcessingStatus
from kb.document_parser import ParseRejected, parse_document


class EpubEncodingTests(unittest.TestCase):
    def container(self, path, raw):
        book = epub.EpubBook()
        book.set_identifier("synthetic-encoding")
        book.set_title("Synthetic")
        book.set_language("en")
        chapters = []
        for ident in ("one", "two"):
            item = epub.EpubHtml(uid=ident, file_name=f"{ident}.xhtml", title=ident)
            item.content = "<p>First valid spine</p>"
            book.add_item(item)
            chapters.append(item)
        book.spine = chapters
        book.toc = chapters
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        epub.write_epub(str(path), book)
        # The writer serializes XHTML; replace ZIP bytes AFTER serialization.
        original = path.read_bytes()
        with ZipFile(io.BytesIO(original)) as source, ZipFile(path, "w") as target:
            for entry in source.infolist():
                content = raw if entry.filename.endswith("/two.xhtml") else source.read(entry)
                target.writestr(entry, content)

    def test_invalid_encoding_refuses_entire_book_in_both_apis(self):
        markup = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Привет</p></body></html>'
        cases = {
            "utf16le": codecs.BOM_UTF16_LE + markup.encode("utf-16-le"),
            "utf16be": codecs.BOM_UTF16_BE + markup.encode("utf-16-be"),
            "malformed_utf8": b"<html><body><p>broken\xff</p></body></html>",
            "nul": b"<html><body><p>broken\x00text</p></body></html>",
            "declared_other": (
                b'<?xml version="1.0" encoding="ISO-8859-1"?><html><body>text</body></html>'
            ),
            "utf16_no_bom": markup.encode("utf-16-le"),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.epub"
            for name, raw in cases.items():
                with self.subTest(encoding=name):
                    self.container(path, raw)
                    with self.assertRaises(ParseRejected) as refused:
                        parse_document(path, "application/epub+zip")
                    self.assertEqual(refused.exception.code, "invalid_epub_encoding")
                    result = parse_epub(path.read_bytes(), source_id=UUID(int=1))
                    self.assertIs(result.processing, ProcessingStatus.FAILED)
                    self.assertEqual(result.fragments, ())
                    self.assertFalse(result.needs_ocr)
                    self.assertEqual([p.code for p in result.problems], ["invalid_epub_encoding"])

    def test_utf8_original_visible_text_and_locations_survive(self):
        markup = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Metadata</title></head>'
            "<body>Привет <b>мир</b><div>Before<p>Nested</p>After</div>Tail</body></html>"
        ).encode()
        expected = ["First valid spine", "Привет мир", "Before", "Nested", "After", "Tail"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.epub"
            for bom in (b"", codecs.BOM_UTF8):
                with self.subTest(bom=bom):
                    self.container(path, bom + markup)
                    rows = parse_document(path, "application/epub+zip")
                    self.assertEqual([r["text"] for r in rows], expected)
                    self.assertEqual(
                        [(r["spine"], r["paragraph"]) for r in rows],
                        [("one", 1)] + [("two", n) for n in range(1, 6)],
                    )
                    self.assertTrue(
                        all(r["file_page"] is None and r["printed_label"] is None for r in rows)
                    )
                    result = parse_epub(path.read_bytes(), source_id=UUID(int=1))
                    self.assertIs(result.processing, ProcessingStatus.DONE)
                    self.assertEqual([f.text for f in result.fragments], expected)
                    self.assertEqual(
                        [(f.locator.spine, f.locator.paragraph) for f in result.fragments],
                        [("1", 1)] + [("2", n) for n in range(1, 6)],
                    )
                    self.assertEqual([f.ordinal for f in result.fragments], list(range(6)))
                    self.assertTrue(
                        all(
                            f.locator.file_page is None and f.locator.printed_label is None
                            for f in result.fragments
                        )
                    )


if __name__ == "__main__":
    unittest.main()
