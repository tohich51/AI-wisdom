"""Lifecycle vocabularies.

Statuses are explicit rather than free-form strings so that the admin UI, the
MCP surface and the worker cannot disagree about what "published" or "done"
means. Every vocabulary is closed: an unknown status is an error, not a
passthrough.
"""

from __future__ import annotations

from enum import StrEnum


class LibraryKind(StrEnum):
    """What a library *is*. Independent of who may see it."""

    REFERENCE = "reference"
    BRAND = "brand"
    PROJECT = "project"
    PLAYBOOK = "playbook"
    EXPERIENCE = "experience"


class LibraryRole(StrEnum):
    """What a person may do inside one library. Never global, never inherited."""

    READER = "reader"
    CONTRIBUTOR = "contributor"
    CURATOR = "curator"
    MANAGER = "manager"


ROLE_RANK: dict[str, int] = {
    LibraryRole.READER.value: 10,
    LibraryRole.CONTRIBUTOR.value: 20,
    LibraryRole.CURATOR.value: 30,
    LibraryRole.MANAGER.value: 40,
}


class ProcessingStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PARTIAL = "partial"
    NEEDS_OCR = "needs_ocr"  # unsupported scan; no OCR is promised in v1
    FAILED = "failed"
    DONE = "done"


class PublicationStatus(StrEnum):
    """Publication is deliberately coarser than processing.

    `NEEDS_REVIEW` exists because extraction surfaces disputed passages for a
    human batch decision rather than silently accepting them.
    """

    DRAFT = "draft"
    NEEDS_REVIEW = "needs_review"
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"


class LocatorKind(StrEnum):
    """Structural locators per PRODUCT-SPEC. Invented page numbers are banned."""

    PDF_FILE_PAGE = "pdf_file_page"
    PDF_PRINTED_LABEL = "pdf_printed_label"
    EPUB_CHAPTER = "epub_chapter"
    EPUB_SPINE = "epub_spine"
    EPUB_PARAGRAPH = "epub_paragraph"
    DOCX_PARAGRAPH = "docx_paragraph"
    DOCX_TABLE = "docx_table"
    DOCX_CELL = "docx_cell"
    HTML_SNAPSHOT = "html_snapshot"
    NONE = "none"


class VerificationStatus(StrEnum):
    """A knowledge claim's verification state. Not the same as confidence."""

    UNVERIFIED = "unverified"
    CITED = "cited"
    DISPUTED = "disputed"
    REFUTED = "refuted"
