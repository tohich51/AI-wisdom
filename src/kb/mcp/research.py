"""Additive Research/Studio/graph tools on the same authenticated transport."""

import json
from typing import Literal
from uuid import UUID

from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from kb.catalog import graphs
from kb.mcp.timing import measure
from kb.research import notes, rule_handoff, runs, semantic, similarity
from kb.research.generation import Kind, Predicate


def install_tools(server, call):
    @server.tool()
    def research_sources(library_id: UUID, ctx: Context):
        return call(ctx, runs.list_research_sources, library_id)

    @server.tool()
    def start_research(
        library_id: UUID,
        source_ids: list[UUID],
        idempotency_key: str,
        ctx: Context,
        kind: Kind = "research",
        query: str = "",
    ):
        return call(ctx, runs.submit_research, library_id, source_ids, kind, query, idempotency_key)

    @server.tool()
    def list_research(library_id: UUID, ctx: Context):
        return call(ctx, runs.list_research, library_id)

    @server.tool()
    def research_status(run_id: UUID, ctx: Context):
        return call(ctx, runs.get_research, run_id)

    @server.tool()
    def save_research_note(run_id: UUID, title: str, ctx: Context, comment: str = ""):
        return call(ctx, notes.save_note, run_id, title, comment)

    @server.tool()
    def list_research_notes(library_id: UUID, ctx: Context):
        return call(ctx, notes.list_notes, library_id)

    @server.tool()
    def read_research_note(note_id: UUID, ctx: Context):
        return call(ctx, notes.get_note, note_id)

    @server.tool()
    def export_research(run_id: UUID, ctx: Context):
        return call(ctx, notes.export_research, run_id)

    @server.tool()
    def draft_rule_from_research(
        run_id: UUID, title: str, when_to_apply: str, expected_effect: str, ctx: Context
    ):
        return call(
            ctx,
            rule_handoff.draft_rule_from_research,
            run_id,
            title,
            when_to_apply,
            expected_effect,
        )

    @server.tool()
    def get_knowledge_graph(
        library_id: UUID, ctx: Context, source_ids: list[UUID] | None = None, limit: int = 200
    ):
        return call(ctx, graphs.provenance_graph, library_id, source_ids, limit)

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def get_semantic_network(library_id: UUID, ctx: Context, limit: int = 200):
        data = call(ctx, semantic.semantic_network, library_id, limit)
        with measure("graph_json_dump"):
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return CallToolResult(content=[TextContent(type="text", text=text)])

    @server.tool()
    def refresh_concept_similarity(library_id: UUID, ctx: Context):
        return call(ctx, similarity.enqueue_similarity, library_id)

    @server.tool()
    def create_semantic_concept(
        library_id: UUID,
        label: str,
        body: str,
        ctx: Context,
        replaces: UUID | None = None,
        idempotency_key: str | None = None,
    ):
        return call(
            ctx,
            semantic.create_concept,
            library_id,
            label,
            body,
            replaces,
            idempotency_key,
        )

    @server.tool()
    def create_semantic_relation(
        library_id: UUID,
        from_concept: UUID,
        to_concept: UUID,
        predicate: Predicate,
        ctx: Context,
        hypothesis: bool = True,
        replaces: UUID | None = None,
        idempotency_key: str | None = None,
    ):
        return call(
            ctx,
            semantic.create_relation,
            library_id,
            from_concept,
            to_concept,
            predicate,
            hypothesis,
            replaces,
            idempotency_key,
        )

    @server.tool()
    def review_semantic_relation(
        entity_id: UUID,
        entity_kind: Literal["concept", "relation"],
        decision: Literal["publish", "withdraw"],
        ctx: Context,
    ):
        return call(ctx, semantic.review_semantic, entity_id, entity_kind, decision)
