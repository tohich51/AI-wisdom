"""Synthetic EPUB fixtures for card C11. Generated here, byte for byte.

An EPUB is a ZIP with a fixed layout, so "synthetic" is easy to guarantee and
also easy to break: every entry below is written by this module, there is no
downloaded file, and no entry contains text that came from anywhere else.
The important thing about a synthetic book for *this* card is that an EPUB has
no pages at all — so the fixture is built to tempt a parser into inventing
some, and the tests then check that none appear.

``booklet.epub``
    Three spine documents (cover, two chapters) with headings, paragraphs, a
    list and a table. A title and a creator that the package metadata states.

``scan.epub``
    One spine document that holds an image and no text: a scanned book wrapped
    in an EPUB container. It must come back ``needs_ocr`` with no fragments,
    exactly like a page-less PDF scan.

``damaged.epub``
    Bytes that are not a ZIP at all — the unsupported-file case.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CONTENT_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:00000000-0000-4000-8000-00000000c011</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:language>en</dc:language>
    {creator}
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="cover"/>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""

NAV_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body>
    <nav epub:type="toc">
      <ol>
        <li><a href="cover.xhtml">Cover</a></li>
        <li><a href="ch1.xhtml">Chapter One</a></li>
        <li><a href="ch2.xhtml#start">Chapter Two</a></li>
      </ol>
    </nav>
  </body>
</html>
"""

COVER_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Cover</title></head>
  <body>
    <h1>A Synthetic Booklet</h1>
    <p>Every byte of this file was written by a test.</p>
  </body>
</html>
"""

CH1_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter One</title></head>
  <body>
    <h1>Chapter One</h1>
    <p>A paragraph that exists only in this synthetic book.</p>
    <p>A second paragraph, with <em>inline emphasis</em> inside it.</p>
    <ul>
      <li>First item of a list</li>
      <li>Second item of a list</li>
    </ul>
  </body>
</html>
"""

CH2_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter Two</title></head>
  <body>
    <h1 id="start">Chapter Two</h1>
    <p>The last chapter of the synthetic booklet.</p>
    <p>Printed on page 12 of a paperback edition that is not this file.</p>
    <table>
      <tr><td>Cell A1</td><td>Cell A2</td></tr>
      <tr><td>Cell B1</td><td>Cell B2</td></tr>
    </table>
  </body>
</html>
"""

SCAN_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Scanned plate</title></head>
  <body>
    <p><img src="plate.png" alt="a scanned plate"/></p>
  </body>
</html>
"""

SCAN_NAV_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body><nav epub:type="toc"><ol><li><a href="scan.xhtml">Plate</a></li></ol></nav></body>
</html>
"""

SCAN_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:00000000-0000-4000-8000-00000000c012</dc:identifier>
    <dc:title>A Scanned Booklet</dc:title>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="scan" href="scan.xhtml" media-type="application/xhtml+xml"/>
    <item id="plate" href="plate.png" media-type="image/png"/>
  </manifest>
  <spine>
    <itemref idref="scan"/>
  </spine>
</package>
"""


def _zip(entries: list[tuple[str, str | bytes]], *, store_mimetype: bool = True) -> bytes:
    """Build the container, honouring the OCF rule that mimetype comes first."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if store_mimetype:
            # OCF: the mimetype entry must be first and stored uncompressed.
            info = zipfile.ZipInfo("mimetype", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, "application/epub+zip")
        for name, content in entries:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


def container_with(
    *, opf: str, nav: str | None = None, documents: dict[str, str] | None = None
) -> bytes:
    """A container whose package and documents a test supplies.

    Used to build the awkward books — a spine entry the manifest does not
    define, an empty spine — out of the same writer that builds the others, so
    a test fixture is never hand-assembled bytes.
    """
    entries: list[tuple[str, str | bytes]] = [("META-INF/container.xml", CONTAINER_XML)]
    if nav is not None:
        entries.append(("OEBPS/nav.xhtml", nav))
    entries.append(("OEBPS/content.opf", opf))
    entries.extend((f"OEBPS/{name}", body) for name, body in (documents or {}).items())
    return _zip(entries)


def booklet_epub() -> bytes:
    """A complete, readable three-document EPUB with a stated creator."""
    return booklet_epub_with_nav(NAV_XHTML, creator="<dc:creator>Ada Fixture</dc:creator>")


def booklet_epub_with_nav(nav: str, *, creator: str = "") -> bytes:
    """The same book with a different table of contents.

    The navigation document is a parameter so a test can build a book whose TOC
    states a title for some documents and none for others — the case where a
    parser is most tempted to invent the missing one.
    """
    return _zip(
        [
            ("META-INF/container.xml", CONTAINER_XML),
            ("OEBPS/content.opf", CONTENT_OPF.format(title="A Synthetic Booklet", creator=creator)),
            ("OEBPS/nav.xhtml", nav),
            ("OEBPS/cover.xhtml", COVER_XHTML),
            ("OEBPS/ch1.xhtml", CH1_XHTML),
            ("OEBPS/ch2.xhtml", CH2_XHTML),
        ]
    )


def anonymous_epub() -> bytes:
    """The same book with no creator anywhere — the author must come back None."""
    return booklet_epub_with_nav(NAV_XHTML, creator="")


def scan_epub() -> bytes:
    """An EPUB whose only spine document is a picture: no text to extract."""
    return container_with(
        opf=SCAN_OPF,
        nav=SCAN_NAV_XHTML,
        documents={"scan.xhtml": SCAN_XHTML, "plate.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 16},
    )


def damaged_epub() -> bytes:
    """Bytes that are not a container at all."""
    return b"PK-not-really\x00\x01\x02 synthetic unsupported file\n" * 3


def all_fixtures() -> dict[str, bytes]:
    """Every committed fixture, for the drift test in ``epub_fixtures_test``."""
    return {
        "booklet.epub": booklet_epub(),
        "anonymous.epub": anonymous_epub(),
        "scan.epub": scan_epub(),
    }
