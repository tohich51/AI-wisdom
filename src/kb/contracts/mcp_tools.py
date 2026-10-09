"""The ten MCP tools named in PRODUCT-SPEC.

One stable endpoint, per-user authorisation on each device, and **no admin or
grant mutation over MCP in v1**. Notice what is absent from every argument
model: there is no ``user_id``, ``actor_id``, ``account_id`` or ``role``.
Identity arrives as a :class:`~kb.contracts.entities.TrustedContext` from the
transport. A caller-supplied principal would be a privilege-escalation
primitive, so the schema does not have a slot to put it in.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kb.contracts.entities import (
    JobStatusResult,
    Knowledge,
    Library,
    Rule,
    Source,
)


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


# 1 ---------------------------------------------------------------- list
class ListLibrariesArgs(ToolArgs):
    include_kinds: (
        list[Literal["reference", "brand", "project", "playbook", "experience"]] | None
    ) = None


class ListLibrariesResult(BaseModel):
    libraries: list[Library]
    role_by_library: dict[UUID, str]


# 2 -------------------------------------------------------------- search
class SearchKnowledgeArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=2000)
    library_ids: list[UUID] | None = None
    limit: int = Field(default=10, ge=1, le=50)
    # Ordinary retrieval must work without cloud generation.
    use_generation: bool = False


class SearchKnowledgeResult(BaseModel):
    hits: list[Knowledge]
    retrieved_only: Literal[True] = True
    note: str = (
        "retrieval means served, not applied and not used; no outcome is implied by these results"
    )


# 3 --------------------------------------------------------------- read
class ReadSourceArgs(ToolArgs):
    source_id: UUID
    from_fragment: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=200)


# 4 ----------------------------------------------------------- get rule
class GetRuleArgs(ToolArgs):
    rule_id: UUID
    version_no: int | None = Field(default=None, ge=1)


class GetRuleResult(BaseModel):
    rule: Rule


# 5 ----------------------------------------------------- project context
class GetProjectContextArgs(ToolArgs):
    project_library_id: UUID
    # Project membership does not grant access to the libraries it links.
    # Requesting them explicitly keeps that boundary visible and auditable.
    include_linked_library_ids: list[UUID] = Field(default_factory=list)


class GetProjectContextResult(BaseModel):
    project: Library
    pinned_rules: list[Rule]
    unresolved_dependencies: list[str] = Field(default_factory=list)


# 6 ---------------------------------------------------------- start use
class StartUseArgs(ToolArgs):
    rule_id: UUID
    version_no: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=2000)


class StartUseResult(BaseModel):
    use_id: UUID
    state: Literal["retrieved"] = "retrieved"


# 7 ------------------------------------------------------ record outcome
class RecordOutcomeArgs(ToolArgs):
    use_id: UUID
    observed_result: str | None = Field(default=None, max_length=4000)
    rating: int | None = Field(default=None, ge=1, le=5)
    rating_basis: str | None = Field(default=None, max_length=2000)
    is_correction: bool = False


# 8 -------------------------------------------------------- submit source
class SubmitSourceArgs(ToolArgs):
    library_id: UUID
    title: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1)
    object_key: str = Field(pattern=r"^[a-z0-9][a-z0-9/_.-]*$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)
    from_url: str | None = None  # SSRF/redirect/IP checks apply server-side


class SubmitSourceResult(BaseModel):
    job_id: UUID
    duplicate_of_existing: bool = False


# 9 ---------------------------------------------------------- job status
class JobStatusArgs(ToolArgs):
    job_id: UUID


JobStatusResult = JobStatusResult  # re-export: the entity result is the result


# 10 -------------------------------------------------------- export skill
class ExportSkillArgs(ToolArgs):
    library_id: UUID
    rule_ids: list[UUID] = Field(min_length=1)
    # The export is a file. It is not installed anywhere by this tool.
    install_to_device: Literal[False] = False


class ExportSkillResult(BaseModel):
    archive_name: str
    bytes: int
    manifest_hash: str


MCP_TOOLS: dict[str, tuple[type[BaseModel], type[BaseModel] | None]] = {
    "list_libraries": (ListLibrariesArgs, ListLibrariesResult),
    "search_knowledge": (SearchKnowledgeArgs, SearchKnowledgeResult),
    "read_source": (ReadSourceArgs, Source),
    "get_rule": (GetRuleArgs, GetRuleResult),
    "get_project_context": (GetProjectContextArgs, GetProjectContextResult),
    "start_use": (StartUseArgs, StartUseResult),
    "record_outcome": (RecordOutcomeArgs, None),
    "submit_source": (SubmitSourceArgs, SubmitSourceResult),
    "job_status": (JobStatusArgs, JobStatusResult),
    "export_skill": (ExportSkillArgs, ExportSkillResult),
}

EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        "list_libraries",
        "search_knowledge",
        "read_source",
        "get_rule",
        "get_project_context",
        "start_use",
        "record_outcome",
        "submit_source",
        "job_status",
        "export_skill",
    }
)

# Mutations that must never be reachable over MCP in v1.
FORBIDDEN_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "grant_role",
        "revoke_role",
        "create_library",
        "delete_library",
        "add_member",
        "set_publication",
        "impersonate",
    }
)

# Field names that would let a caller assert identity.
FORBIDDEN_ARG_FIELDS: frozenset[str] = frozenset(
    {
        "user_id",
        "actor_id",
        "account_id",
        "principal_id",
        "role",
        "is_admin",
        "bypass_rls",
    }
)


def assert_no_identity_injection() -> None:
    """Fail loudly if any tool argument grows a caller-asserted identity field."""
    for tool, (args, _result) in MCP_TOOLS.items():
        for field in args.model_fields:
            if field in FORBIDDEN_ARG_FIELDS:
                raise ValueError(
                    f"MCP tool {tool!r} exposes identity field {field!r}; "
                    "identity must come from the trusted transport only"
                )
    overlap = EXPECTED_TOOLS - set(MCP_TOOLS)
    if overlap:
        raise ValueError(f"missing MCP tools declared in PRODUCT-SPEC: {sorted(overlap)}")
    leaked = FORBIDDEN_MCP_TOOLS & set(MCP_TOOLS)
    if leaked:
        raise ValueError(f"admin/grant mutation exposed over MCP: {sorted(leaked)}")
