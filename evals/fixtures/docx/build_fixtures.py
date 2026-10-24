#!/usr/bin/env python3
"""Build the synthetic DOCX fixtures used by ``tests/integration/parsers/docx``.

Every fixture here is generated, not collected. None of them is a real
document, none of them carries a name that could be mistaken for somebody's
material, and none of them exists anywhere but in this directory. They are
committed as binaries so the tests exercise real OOXML — python-docx writing a
real package, and a real ZIP with a real ``[Content_Types].xml`` — rather than
a dict shaped like one.

Run it from the repository root::

    python evals/fixtures/docx/build_fixtures.py

Writing them by hand instead would mean either shipping a real document or
hand-assembling XML that has never been through a Word-compatible writer. The
four files cover the four things this parser must get right:

``basic.docx``          headings, paragraphs, a blank paragraph in the middle,
                        a plain table. The numbering rule.
``nested_table.docx``   a table inside a cell of another table.
``merged_cells.docx``   a horizontal ``w:gridSpan`` merge and a vertical
                        ``w:vMerge`` chain in one table.
``exotic_runs.docx``    hyperlink, tracked insertion, tracked deletion, a field
                        result, an inline content control, a text box, a body
                        level content control, an ``mc:AlternateContent`` and an
                        ``altChunk`` — the constructs the parser reports instead
                        of guessing at.
"""

from __future__ import annotations

import pathlib
import sys
import zipfile

from docx import Document
from lxml import etree

#: ZIP stores a modification time per entry and takes it from the clock. Left
#: alone, the fixtures would differ on every rebuild by however many seconds
#: separated the two runs, and "the committed fixture is the one the builder
#: produces" would be untestable. 1980-01-01 is the earliest a ZIP can express.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
V = "urn:schemas-microsoft-com:vml"

HERE = pathlib.Path(__file__).resolve().parent

_EXOTIC = f"""<w:p xmlns:w="{W}" xmlns:r="{R}">
  <w:r><w:t xml:space="preserve">plain </w:t></w:r>
  <w:hyperlink r:id="rId100"><w:r><w:t>hyperlink text</w:t></w:r></w:hyperlink>
  <w:ins w:id="900" w:author="fixture" w:date="2024-01-01T00:00:00Z">
    <w:r><w:t xml:space="preserve"> inserted text</w:t></w:r>
  </w:ins>
  <w:del w:id="901" w:author="fixture" w:date="2024-01-01T00:00:00Z">
    <w:r><w:delText>deleted text</w:delText></w:r>
  </w:del>
  <w:sdt><w:sdtPr/><w:sdtContent>
    <w:r><w:t xml:space="preserve"> inline control</w:t></w:r>
  </w:sdtContent></w:sdt>
  <w:fldSimple w:instr=" PAGE "><w:r><w:t>7</w:t></w:r></w:fldSimple>
  <w:r><w:instrText>PAGE</w:instrText></w:r>
  <w:r><w:t xml:space="preserve"> after</w:t><w:tab/><w:br/></w:r>
  <w:r>
    <w:pict>
      <v:shape xmlns:v="{V}" id="s1" type="#_x0000_t202" style="width:100pt;height:50pt">
        <v:textbox>
          <w:txbxContent><w:p><w:r><w:t>text box content</w:t></w:r></w:p></w:txbxContent>
        </v:textbox>
      </v:shape>
    </w:pict>
  </w:r>
</w:p>"""

_BODY_CONTROL = f"""<w:sdt xmlns:w="{W}">
  <w:sdtPr><w:alias w:val="body control"/></w:sdtPr>
  <w:sdtContent>
    <w:p><w:r><w:t>paragraph inside a body level control</w:t></w:r></w:p>
  </w:sdtContent>
</w:sdt>"""

_ALTERNATE = f"""<w:p xmlns:w="{W}" xmlns:mc="{MC}" xmlns:r="{R}">
  <w:r><w:t>before alternate </w:t></w:r>
  <mc:AlternateContent>
    <mc:Choice Requires="wps">
      <w:r><w:t>CHOICE-BRANCH</w:t></w:r>
    </mc:Choice>
    <mc:Fallback>
      <w:r><w:t>FALLBACK-BRANCH</w:t></w:r>
    </mc:Fallback>
  </mc:AlternateContent>
  <w:r><w:t> after alternate</w:t></w:r>
</w:p>"""


def _insert_before_sectpr(document: Document, xml: str) -> None:
    body = document.element.body
    body.insert(len(body) - 1, etree.fromstring(xml))


def _basic() -> Document:
    d = Document()
    d.add_heading("Brand voice", level=1)
    d.add_paragraph("Use the approved typeface in every derived artefact.")
    d.add_paragraph("")  # a blank paragraph: it must still consume number 3
    d.add_paragraph("Do not recolour the primary mark.")
    table = d.add_table(rows=2, cols=3)
    for r in range(2):
        for c in range(3):
            table.cell(r, c).text = f"r{r + 1}c{c + 1}"
    d.add_heading("Colour", level=2)
    d.add_paragraph("Primary ink is #101820.")
    return d


def _nested() -> Document:
    d = Document()
    d.add_paragraph("Two tables, one of them inside a cell of the other.")
    outer = d.add_table(rows=2, cols=2)
    outer.cell(0, 0).text = "outer 1,1"
    outer.cell(0, 1).text = "outer 1,2"
    outer.cell(1, 0).text = "outer 2,1"
    inner = outer.cell(1, 1).add_table(rows=2, cols=2)
    for r in range(2):
        for c in range(2):
            inner.cell(r, c).text = f"inner r{r + 1}c{c + 1}"
    outer.cell(1, 1).add_paragraph("and text after the nested table")
    return d


def _merged() -> Document:
    d = Document()
    d.add_paragraph(
        "One horizontal merge on the first row, one vertical merge down the first column."
    )
    t = d.add_table(rows=3, cols=3)
    # Merge first, then write: python-docx concatenates the text of every cell in
    # the merged region, so writing first would leave the anchor holding three
    # paragraphs of whatever happened to be in them.
    t.cell(0, 1).merge(t.cell(0, 2))  # gridSpan=2, anchored at grid column 2
    t.cell(1, 0).merge(t.cell(2, 0))  # w:vMerge restart + two continuations
    t.cell(0, 0).text = "r1c1"
    t.cell(0, 1).text = "spans two columns"
    t.cell(1, 0).text = "merged down three rows"
    t.cell(1, 1).text = "r2c2"
    t.cell(1, 2).text = "r2c3"
    t.cell(2, 1).text = "r3c2"
    t.cell(2, 2).text = "r3c3"
    return d


def _exotic() -> Document:
    d = Document()
    d.add_heading("Fixtures", level=1)
    d.add_paragraph("An ordinary paragraph, so numbering starts at two.")
    _insert_before_sectpr(d, _EXOTIC)
    _insert_before_sectpr(d, _BODY_CONTROL)
    _insert_before_sectpr(d, _ALTERNATE)
    d.add_paragraph("A final ordinary paragraph.")
    return d


BUILDERS = {
    "basic.docx": _basic,
    "nested_table.docx": _nested,
    "merged_cells.docx": _merged,
    "exotic_runs.docx": _exotic,
}


def _normalise_archive(path: pathlib.Path) -> None:
    """Rewrite the package with a fixed timestamp so the build is reproducible."""
    with zipfile.ZipFile(path) as source:
        members = [(info.filename, source.read(info.filename)) for info in source.infolist()]
    staged = path.with_name(path.name + ".staged")
    with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as out:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            out.writestr(info, payload)
    staged.replace(path)


def _save(document, path: pathlib.Path) -> None:
    document.save(str(path))
    _normalise_archive(path)


def build_all(target: pathlib.Path = HERE) -> list[pathlib.Path]:
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, builder in BUILDERS.items():
        path = target / name
        _save(builder(), path)
        written.append(path)
    return written


if __name__ == "__main__":
    out = HERE
    if len(sys.argv) > 1:
        out = pathlib.Path(sys.argv[1])
        out.mkdir(parents=True, exist_ok=True)
    for produced in build_all(out):
        print(f"wrote {produced} ({produced.stat().st_size} bytes)")
