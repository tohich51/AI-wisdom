"""Entity contracts — the single source of truth.

Both the FastAPI routes and the MCP tool handlers construct and return these
models. There is no second schema, and no DTO defined at an interface edge.
`docs/user-guide` and the export generator read these same objects.

Rules encoded here, not merely documented:

* Missing provenance stays ``None``. A model that fills an author it did not
  read is producing fabricated provenance.
* A :class:`Rule` is immutable once published. Correction is a new version,
  never an update in place.
* :class:`Experience` pins ``rule_version_id`` to one exact version. It never
  points at "the current rule".
* Actor and account identity are supplied by the *trusted* transport, not by
  the request payload. See :class:`TrustedContext`; no request model has a
  field for it.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kb.contracts.enums import (
    LibraryKind,
    LibraryRole,
    LocatorKind,
    ProcessingStatus,
    PublicationStatus,
    VerificationStatus,
)
from kb.domain.tiers import UsageState

UTC = dt.UTC


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------- provenance


class Locator(Contract):
    """Where in the original something came from.

    ``kind`` distinguishes a file page from a printed label, because the two
    disagree in most real books and collapsing them is how fake citations get
    made.
    """

    kind: LocatorKind
    file_page: int | None = Field(default=None, ge=1)
    printed_label: str | None = None
    chapter: str | None = None
    spine: str | None = None
    paragraph: int | None = Field(default=None, ge=1)
    table: int | None = Field(default=None, ge=1)
    cell: tuple[int, int] | None = None
    snapshot_url: str | None = None
    snapshot_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _page_number_not_invented(self) -> Locator:
        if self.kind is LocatorKind.PDF_PRINTED_LABEL and self.file_page is not None:
            raise ValueError(
                "a printed label is not a file page; do not substitute one for the other"
            )
        if self.kind is LocatorKind.PDF_FILE_PAGE and self.file_page is None:
            raise ValueError("pdf_file_page requires file_page")
        return self


class Provenance(Contract):
    """Every field is optional and may be genuinely unknown.

    There is no ``"unknown"`` string: absence is represented by ``None`` so
    that it cannot be mistaken for a value.
    """

    source_id: UUID
    locator: Locator | None = None
    author: str | None = None
    publication: str | None = None
    url: str | None = None
    retrieved_at: dt.datetime | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------- entities


class Library(Contract):
    id: UUID
    name: str = Field(min_length=1, max_length=200)
    kind: LibraryKind
    # Audience is independent of kind: a brand library may be project-private
    # and a reference library may be open to all invited users.
    audience_scope: Literal["private", "invited", "all_invited"] = "private"
    created_at: dt.datetime = Field(default_factory=_now)
    generation: int = Field(default=1, ge=1)


class Grant(Contract):
    """Per-library role. Membership in a project does NOT confer access to the
    source libraries it links — see ACCESS-MODEL."""

    library_id: UUID
    principal_id: UUID
    role: LibraryRole


class Source(Contract):
    id: UUID
    library_id: UUID
    title: str = Field(min_length=1)
    submitted_by: UUID
    media_type: str
    # Immutable object key. The bytes are addressed by content, never by a
    # mutable path.
    object_key: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9/_.-]*$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    processing: ProcessingStatus = ProcessingStatus.QUEUED
    publication: PublicationStatus = PublicationStatus.DRAFT
    created_at: dt.datetime = Field(default_factory=_now)


class Fragment(Contract):
    """A located piece of a source, at a stable structural address."""

    id: UUID
    source_id: UUID
    ordinal: int = Field(ge=0)
    locator: Locator
    text: str


class SourceVersion(Contract):
    """A new ingest of the same logical source. Superseded, never mutated."""

    id: UUID
    source_id: UUID
    version_no: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: dt.datetime = Field(default_factory=_now)


class Knowledge(Contract):
    id: UUID
    library_id: UUID
    statement: str = Field(min_length=1)
    kind: Literal["assertion", "definition", "recommendation", "example"]
    provenance: list[Provenance] = Field(default_factory=list)
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    publication: PublicationStatus = PublicationStatus.DRAFT


class Rule(Contract):
    """Immutable once published.

    ``version_no`` increments on correction; the previous version stays
    readable because an Experience may already point at it.
    """

    id: UUID
    library_id: UUID
    version_no: int = Field(ge=1)
    immutable: bool = True
    title: str = Field(min_length=1)
    when_to_apply: str | None = None  # null when genuinely unspecified
    inputs: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    expected_effect: str | None = None
    verification: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    publication: PublicationStatus = PublicationStatus.DRAFT

    @model_validator(mode="after")
    def _published_rule_is_complete_enough_to_apply(self) -> Rule:
        if self.publication is PublicationStatus.PUBLISHED and not (
            self.when_to_apply and self.actions and self.expected_effect
        ):
            raise ValueError(
                "a published rule needs when_to_apply, actions and expected_effect; "
                "otherwise it is a draft with holes"
            )
        return self


class Use(Contract):
    """A consumer acting on a rule. Creation is not proof of application."""

    id: UUID
    rule_version_id: UUID
    principal_id: UUID
    state: UsageState = UsageState.RETRIEVED
    started_at: dt.datetime = Field(default_factory=_now)


class Experience(Contract):
    """Who applied which exact rule version, where, and what they expected.

    Pinned to a version id, never to a rule id alone: a rule gets corrected,
    and an outcome recorded against "the rule" becomes unattributable.
    """

    id: UUID
    use_id: UUID
    rule_version_id: UUID
    where: str | None = None
    expected_result: str | None = None
    created_at: dt.datetime = Field(default_factory=_now)


class Outcome(Contract):
    """Written only after someone reports what actually happened."""

    id: UUID
    use_id: UUID
    observed_result: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    rating_basis: str | None = None
    reported_by: UUID | None = None  # a metric has no human author
    reported_at: dt.datetime | None = None
    is_correction: bool = False


class Job(Contract):
    id: UUID
    library_id: UUID
    kind: Literal["ingest", "extract", "embed", "compile", "export"]
    status: ProcessingStatus = ProcessingStatus.QUEUED
    # Idempotency: a retried submission must not create a second job.
    idempotency_key: str = Field(min_length=1)
    index_target: str | None = None
    index_generation: int = Field(default=1, ge=1)


class IndexTarget(Contract):
    """OpenViking is a rebuildable projection, not the source of truth."""

    name: str
    generation: int = Field(ge=1)
    account: str
    is_current: bool = False


# --------------------------------------------------------- trusted context


class TrustedContext(Contract):
    """Identity established by the transport (Keycloak token, MCP auth).

    This type is constructed by the *server*. No request body, query parameter
    or MCP tool argument maps to it. A ``user_id`` field supplied by a caller
    is data, not identity — see ACCESS-MODEL.
    """

    principal_id: UUID
    account_id: UUID
    roles_by_library: dict[UUID, LibraryRole] = Field(default_factory=dict)
    generation_watermark: int = Field(ge=1)


# ------------------------------------------------------------ job results


class JobStatusResult(Contract):
    job: Job
    progress: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    message: str | None = None
    disputed_passages: list[str] = Field(default_factory=list)
