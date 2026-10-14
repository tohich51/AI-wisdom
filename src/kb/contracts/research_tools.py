"""Strict shared arguments for browser routes and additive MCP capabilities."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from kb.model_research import Kind, Predicate


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LibraryArgs(Args):
    library_id: UUID


class ResearchArgs(LibraryArgs):
    source_ids: list[UUID] = Field(min_length=1, max_length=10)
    kind: Kind = "research"
    query: str = Field(default="", max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class RunArgs(Args):
    run_id: UUID


class SaveNoteArgs(RunArgs):
    title: str = Field(min_length=1, max_length=500)
    comment: str = Field(default="", max_length=8000)


class NoteArgs(Args):
    note_id: UUID


class UpdateNoteArgs(Args):
    title: str = Field(min_length=1, max_length=500)
    comment: str = Field(default="", max_length=8000)
    revision: int = Field(ge=1, strict=True)


class DraftRuleArgs(RunArgs):
    title: str = Field(min_length=1, max_length=500)
    when_to_apply: str = Field(min_length=1, max_length=2000)
    expected_effect: str = Field(min_length=1, max_length=2000)


class GraphArgs(LibraryArgs):
    source_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=10)
    limit: int = Field(default=200, ge=1, le=500)


class SemanticArgs(LibraryArgs):
    limit: int = Field(default=200, ge=1, le=500)


class ConceptArgs(LibraryArgs):
    label: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=2000)
    replaces: UUID | None = None


class RelationArgs(LibraryArgs):
    from_concept: UUID
    to_concept: UUID
    predicate: Predicate
    hypothesis: bool = Field(default=True, strict=True)


class ReviewArgs(Args):
    entity_id: UUID
    entity_kind: Literal["concept", "relation"]
    decision: Literal["publish", "withdraw"]


RESEARCH_TOOLS: dict[str, type[BaseModel]] = {
    "research_sources": LibraryArgs,
    "list_research": LibraryArgs,
    "start_research": ResearchArgs,
    "research_status": RunArgs,
    "save_research_note": SaveNoteArgs,
    "list_research_notes": LibraryArgs,
    "read_research_note": NoteArgs,
    "export_research": RunArgs,
    "draft_rule_from_research": DraftRuleArgs,
    "get_knowledge_graph": GraphArgs,
    "get_semantic_network": SemanticArgs,
    "create_semantic_concept": ConceptArgs,
    "create_semantic_relation": RelationArgs,
    "review_semantic_relation": ReviewArgs,
}
