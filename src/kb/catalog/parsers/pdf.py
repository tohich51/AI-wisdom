"""PDF → normalised fragments with exact structural locators (card C11).

The single thing this module exists to guarantee:

    a PDF **file page** and a **printed page label** are different things.

They disagree in almost every real book — front matter in roman numerals is
the obvious case — and collapsing the two is how a citation becomes fiction.
So every fragment produced here carries

* ``locator.kind = pdf_file_page`` and ``locator.file_page`` = the 1-based
  position of the page in the file, and
* ``locator.printed_label`` = the label the document itself declares for that
  page, **only if the document declares one**.

A document that declares no ``/PageLabels`` number tree has no printed labels.
pypdf will happily answer ``"1"``, ``"2"``, … in that case, but those answers
are its own fallback, not something anybody printed on the page, so they are
dropped here rather than recorded as provenance. A label that happens to be
readable in the page's text is a guess about layout, not a read, and is not
attempted either.

The parser is **pypdf** (see ``PARSER_VERSION``), a real library doing a real
parse of the real bytes. There is no OCR and none is faked: a page with no text
layer is reported as a problem, and a document in which *no* page has a text
layer comes back ``needs_ocr`` with **zero** fragments, because a scan has no
text to extract and inventing some is worse than admitting there is none.
Full OCR/vision is explicitly not promised in v1.

The normalised output is :class:`kb.contracts.entities.Fragment` — the same
contract the HTTP and MCP surfaces use. :func:`fragment_row` maps one onto the
``kb.fragment`` columns so a caller can persist exactly what was parsed.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pypdf

from kb.contracts.entities import Fragment, Locator
from kb.contracts.enums import LocatorKind, ProcessingStatus

__all__ = [
    "PARSER_NAME",
    "PARSER_VERSION",
    "PdfParseResult",
    "PdfProblem",
    "fragment_row",
    "parse_pdf",
    "text_at_locator",
]

PARSER_NAME = "pypdf"
PARSER_VERSION: str = pypdf.__version__

MEDIA_TYPE = "application/pdf"

#: A page counts as having a text layer when the text layer yields at least one
#: non-whitespace character. The threshold is deliberately *one character* and
#: not a density heuristic: a longer threshold would discard real text (a
#: full-page plate caption, a page that carries nothing but a dropped-cap
#: opening), and a discarded real page is a silent hole in the book. What this
#: threshold detects is exactly the thing the card is about — a page whose
#: bytes are a picture, not a text layer.
TEXT_LAYER_MIN_CHARS = 1


@dataclass(frozen=True, slots=True)
class PdfProblem:
    """A visible, named reason the parse is not a clean success.

    Problems are data, not log lines. They survive into the result so the
    review queue can show *why* a page is missing instead of the page quietly
    not existing.
    """

    code: str
    detail: str
    file_page: int | None = None


@dataclass(frozen=True, slots=True)
class PdfParseResult:
    """Everything one parse produced, including what it could not produce."""

    parser: str
    parser_version: str
    media_type: str
    processing: ProcessingStatus
    page_count: int
    fragments: tuple[Fragment, ...]
    problems: tuple[PdfProblem, ...]
    title: str | None = None
    author: str | None = None

    @property
    def pages_without_text(self) -> tuple[int, ...]:
        return tuple(
            p.file_page for p in self.problems if p.code == "no_text_layer" and p.file_page
        )

    @property
    def needs_ocr(self) -> bool:
        return self.processing is ProcessingStatus.NEEDS_OCR


def parse_pdf(data: bytes, *, source_id: UUID) -> PdfParseResult:
    """Parse a PDF into one fragment per file page.

    A page whose text layer is empty produces **no** fragment. The page is
    still counted, and the reason is in :attr:`PdfParseResult.problems`.
    """
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
    except Exception as exc:  # pypdf raises a family of unrelated types here
        return _failure(
            source_id,
            "unreadable_pdf",
            f"pypdf {PARSER_VERSION} could not open the file: {exc}",
        )

    if reader.is_encrypted:
        accepted: bool
        try:
            accepted = bool(reader.decrypt(""))
        except Exception:
            accepted = False
        if not accepted:
            return _failure(
                source_id,
                "encrypted_pdf",
                "the file is encrypted and no empty password was accepted; "
                "no page was read and no text was guessed",
            )

    try:
        page_count = len(reader.pages)
    except Exception as exc:
        return _failure(source_id, "damaged_pdf", f"the page tree could not be read: {exc}")

    if page_count == 0:
        return _failure(
            source_id,
            "no_pages",
            "the file declares zero pages; there is no page to read and "
            "'needs_ocr' would claim a scanned page that does not exist",
        )

    labels = _declared_labels(reader, page_count)
    fragments: list[Fragment] = []
    problems: list[PdfProblem] = []
    pages_with_text = 0

    for index in range(page_count):
        file_page = index + 1
        try:
            page = reader.pages[index]
            raw = page.extract_text() or ""
            has_images = _page_has_images(page)
        except Exception as exc:
            # One unreadable page must not lose the rest of the book.
            raw = ""
            has_images = False
            problems.append(
                PdfProblem(
                    code="unreadable_page",
                    detail=f"the page could not be read: {exc}",
                    file_page=file_page,
                )
            )

        text = _normalise(raw)
        if len(text.replace(" ", "")) < TEXT_LAYER_MIN_CHARS:
            problems.append(
                PdfProblem(
                    code="no_text_layer",
                    detail=(
                        "the page carries an image and no text layer; v1 does not "
                        "promise OCR, so no text was produced for it"
                        if has_images
                        else "the page yielded no text and no image; nothing was read from it"
                    ),
                    file_page=file_page,
                )
            )
            continue

        pages_with_text += 1
        fragments.append(
            Fragment(
                id=uuid.uuid4(),
                source_id=source_id,
                ordinal=index,
                locator=Locator(
                    kind=LocatorKind.PDF_FILE_PAGE,
                    file_page=file_page,
                    printed_label=labels[index],
                ),
                text=text,
            )
        )

    if pages_with_text == 0:
        # A scan. Zero fragments, on purpose: there is no text to record.
        processing = ProcessingStatus.NEEDS_OCR
    elif pages_with_text < page_count:
        processing = ProcessingStatus.PARTIAL
    else:
        processing = ProcessingStatus.DONE

    return PdfParseResult(
        parser=PARSER_NAME,
        parser_version=PARSER_VERSION,
        media_type=MEDIA_TYPE,
        processing=processing,
        page_count=page_count,
        fragments=tuple(fragments),
        problems=tuple(problems),
        title=_info_text(reader, "/Title"),
        author=_info_text(reader, "/Author"),
    )


def text_at_locator(data: bytes, locator: Locator) -> str | None:
    """Re-open the file and return the text this locator points at.

    ``None`` means "this locator does not name one addressable piece of text" —
    a locator kind this parser does not serve, a page outside the file, a
    printed label the document never declared, or a printed label that several
    pages share. Ambiguity returns ``None`` rather than picking the first
    match: guessing which of two identical-looking pages a citation meant is
    exactly the failure this card exists to prevent.
    """
    if locator.kind is LocatorKind.PDF_FILE_PAGE:
        target = locator.file_page
    elif locator.kind is LocatorKind.PDF_PRINTED_LABEL:
        if not locator.printed_label:
            return None
        target = _page_for_label(data, locator.printed_label)
    else:
        return None

    if target is None:
        return None
    for fragment in parse_pdf(data, source_id=uuid.UUID(int=0)).fragments:
        if fragment.locator.file_page == target:
            return fragment.text
    return None


def _page_for_label(data: bytes, label: str) -> int | None:
    """The one file page this document declares this printed label for."""
    matches = [
        f.locator.file_page
        for f in parse_pdf(data, source_id=uuid.UUID(int=0)).fragments
        if f.locator.printed_label == label and f.locator.file_page is not None
    ]
    return matches[0] if len(matches) == 1 else None


def fragment_row(fragment: Fragment) -> dict[str, Any]:
    """The ``kb.fragment`` column values for one parsed fragment.

    Every column of the locator is carried across. Nothing is defaulted: a
    column whose value the parser did not read stays ``None``, which is what
    "unknown" means in this schema.
    """
    return {
        "id": fragment.id,
        "source_id": fragment.source_id,
        "ordinal": fragment.ordinal,
        "locator_kind": fragment.locator.kind.value,
        "file_page": fragment.locator.file_page,
        "printed_label": fragment.locator.printed_label,
        "chapter": fragment.locator.chapter,
        "paragraph": fragment.locator.paragraph,
        "text": fragment.text,
    }


# --------------------------------------------------------------------- bits


def _failure(source_id: UUID, code: str, detail: str) -> PdfParseResult:
    return PdfParseResult(
        parser=PARSER_NAME,
        parser_version=PARSER_VERSION,
        media_type=MEDIA_TYPE,
        processing=ProcessingStatus.FAILED,
        page_count=0,
        fragments=(),
        problems=(PdfProblem(code=code, detail=detail),),
    )


def _normalise(raw: str) -> str:
    """Whitespace-normalise extracted text.

    pypdf's layout extraction returns the text with the line breaks the content
    stream happened to have, plus a trailing newline per page. Lines are
    stripped, blank lines are dropped and the rest are joined with a newline,
    so the same text on two pages of the same book compares equal.
    """
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _declared_labels(reader: Any, page_count: int) -> list[str | None]:
    """The printed label each page declares, or ``None`` for all of them.

    A PDF carries printed page labels in the optional ``/PageLabels`` number
    tree of the catalog. Without that tree the document declares nothing, and
    pypdf substitutes the page position — which is a file page wearing a
    printed label's clothes. Those substituted values are dropped, so the
    printed_label column stays empty instead of echoing file_page back at
    itself and looking like corroboration.
    """
    root = getattr(reader, "root_object", None)
    try:
        declares = root is not None and "/PageLabels" in root
    except Exception:
        declares = False
    if not declares:
        return [None] * page_count
    try:
        labels: Sequence[str] = reader.page_labels
    except Exception:
        return [None] * page_count
    return [labels[i] if i < len(labels) and labels[i] else None for i in range(page_count)]


def _page_has_images(page: Any) -> bool:
    try:
        resources = page.get("/Resources")
        if resources is None:
            return False
        resources = resources.get_object()
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return False
        for xobject in xobjects.get_object().values():
            if xobject.get_object().get("/Subtype") == "/Image":
                return True
    except Exception:
        return False
    return False


def _info_text(reader: Any, key: str) -> str | None:
    """A document-info string, or ``None`` when the document has none.

    An author is a piece of provenance, so it is only ever reported when the
    file actually carries it. There is no fallback to the file name, to the
    submitting user, or to a placeholder.
    """
    try:
        metadata = reader.metadata
        if metadata is None:
            return None
        value = metadata.get(key)
    except Exception:
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None
