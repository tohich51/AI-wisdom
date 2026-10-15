"""Official MCP transport; catalog calls share authenticated PostgreSQL identity."""

import os
from typing import Literal
from uuid import UUID

import anyio
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent
from pydantic import AnyHttpUrl, ValidationError

from kb.access.policy import Principal
from kb.catalog import content, libraries, projects, upload, writes
from kb.contracts.enums import LibraryKind
from kb.contracts.mcp_tools import (
    MCP_TOOLS,
    ExportSkillArgs,
    GetProjectContextArgs,
    GetRuleArgs,
    JobStatusArgs,
    ListLibrariesArgs,
    PublishKnowledgeArgs,
    ReadSourceArgs,
    RecordOutcomeArgs,
    SearchKnowledgeArgs,
    StartUseArgs,
    SubmitSourceArgs,
)
from kb.ingestion import submit_processing


def build_mcp(app, hosts):
    resource = os.environ["KB_MCP_RESOURCE_URL"]

    class Verifier:
        async def verify_token(self, token):
            try:
                identity = await anyio.to_thread.run_sync(app.state.resolve_token, token)
                return AccessToken(
                    token=token,
                    client_id=os.environ["KB_MCP_CLIENT_ID"],
                    scopes=list(identity.scopes),
                    subject=identity.subject,
                    expires_at=int(identity.expires_at.timestamp()),
                    resource=resource,
                    claims={"iss": identity.issuer},
                )
            except HTTPException:
                return None

    server = MCPServer(
        "AI-wisdom",
        token_verifier=Verifier(),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(os.environ["KB_OIDC_ISSUER"]),
            resource_server_url=AnyHttpUrl(resource),
            validate_token_resource=True,
            required_scopes=os.environ.get("KB_OIDC_REQUIRED_SCOPES", "").split(),
        ),
    )

    async def strict_arguments(ctx, call_next):
        if ctx.method == "tools/call":
            params = ctx.params or {}
            model = MCP_TOOLS.get(params.get("name"), (None, None))[0]
            try:
                if model is None:
                    raise ValueError("unknown_tool")
                model.model_validate(params.get("arguments") or {})
            except (ValueError, ValidationError):
                return CallToolResult(
                    isError=True, content=[TextContent(type="text", text="invalid_tool_arguments")]
                )
        return await call_next(ctx)

    server.middleware.append(strict_arguments)

    def call(ctx, handler, *args, **kwargs):
        request = ctx.request_context.request
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise ValueError("authentication_required")
        # Re-check current membership on every tool call, even an existing session.
        identity = app.state.resolve_token(header[7:])
        principal = Principal(identity.principal_id, identity.account_id)
        try:
            with app.state.db_pool.connection() as conn:
                return jsonable_encoder(handler(conn, principal, *args, **kwargs))
        except Exception:
            raise ValueError("request_refused") from None

    from kb.mcp.research import install_tools

    install_tools(server, call)

    @server.tool()
    def list_libraries(
        ctx: Context,
        include_kinds: list[Literal["reference", "brand", "project", "playbook", "experience"]]
        | None = None,
    ):
        args = ListLibrariesArgs(include_kinds=include_kinds)
        return call(
            ctx,
            libraries.list_libraries,
            kinds=[LibraryKind(k) for k in args.include_kinds] if args.include_kinds else None,
        )

    @server.tool()
    def search_knowledge(
        query: str,
        ctx: Context,
        library_ids: list[UUID] | None = None,
        limit: int = 10,
        use_generation: bool = False,
    ):
        args = SearchKnowledgeArgs(
            query=query, library_ids=library_ids, limit=limit, use_generation=use_generation
        )
        if args.use_generation:
            raise ValueError("generation_is_not_retrieval")
        return call(
            ctx,
            content.search_knowledge,
            args.query,
            args.library_ids,
            args.limit,
            semantic=getattr(app.state, "semantic_search", None),
        )

    @server.tool()
    def read_source(source_id: UUID, ctx: Context, from_fragment: int = 0, limit: int = 20):
        args = ReadSourceArgs(source_id=source_id, from_fragment=from_fragment, limit=limit)
        return call(ctx, content.read_source, args.source_id, args.from_fragment, args.limit)

    @server.tool()
    def get_rule(rule_id: UUID, ctx: Context, version_no: int | None = None):
        args = GetRuleArgs(rule_id=rule_id, version_no=version_no)
        return {"rule": call(ctx, content.get_rule, args.rule_id, args.version_no)}

    @server.tool()
    def publish_knowledge(knowledge_id: UUID, ctx: Context):
        """Publish an existing knowledge draft only after reviewing its source evidence."""
        args = PublishKnowledgeArgs(knowledge_id=knowledge_id)
        return call(ctx, writes.publish, args.knowledge_id, knowledge=True)

    @server.tool()
    def get_project_context(
        project_library_id: UUID, ctx: Context, include_linked_library_ids: list[UUID] | None = None
    ):
        args = GetProjectContextArgs(
            project_library_id=project_library_id,
            include_linked_library_ids=include_linked_library_ids or [],
        )
        result = call(ctx, projects.project_context, args.project_library_id)
        if result is None:
            raise ValueError("request_refused")
        from kb.catalog.projects import ProjectContext

        context = ProjectContext.model_validate(result)
        linked = {link.library.id for link in context.links if link.library is not None}
        if set(args.include_linked_library_ids) - linked:
            raise ValueError("request_refused")
        return jsonable_encoder(context.to_mcp_result())

    @server.tool()
    def start_use(
        rule_id: UUID,
        version_no: int,
        ctx: Context,
        note: str | None = None,
        expected_result: str | None = None,
        where_applied: str | None = None,
    ):
        args = StartUseArgs(
            rule_id=rule_id,
            version_no=version_no,
            note=note,
            expected_result=expected_result,
            where_applied=where_applied,
        )
        result = call(
            ctx,
            writes.start_use,
            args.rule_id,
            args.version_no,
            args.expected_result,
            args.where_applied,
        )
        return {"use_id": result["id"], "state": result["state"]}

    @server.tool()
    def record_outcome(
        use_id: UUID,
        ctx: Context,
        observed_result: str | None = None,
        rating: int | None = None,
        rating_basis: str | None = None,
        is_correction: bool = False,
        confirm_applied: bool = False,
    ):
        args = RecordOutcomeArgs(
            use_id=use_id,
            observed_result=observed_result,
            rating=rating,
            rating_basis=rating_basis,
            is_correction=is_correction,
            confirm_applied=confirm_applied,
        )
        if args.confirm_applied:
            call(ctx, writes.mark_applied, args.use_id)
        return call(
            ctx,
            writes.record_outcome,
            args.use_id,
            args.observed_result,
            args.rating,
            args.rating_basis,
            args.is_correction,
        )

    @server.tool()
    def submit_source(
        library_id: UUID,
        title: str,
        idempotency_key: str,
        ctx: Context,
        source_id: UUID | None = None,
        from_url: str | None = None,
        extract: bool = False,
    ):
        args = SubmitSourceArgs(
            library_id=library_id,
            title=title,
            idempotency_key=idempotency_key,
            source_id=source_id,
            from_url=from_url,
            extract=extract,
        )
        if args.from_url:
            result = call(
                ctx,
                upload.submit_url,
                app.state.blob_store,
                upload.SubmitUrl(
                    library_id=library_id,
                    url=args.from_url,
                    title=title,
                    idempotency_key=idempotency_key,
                ),
            )
            source_id = UUID(result["source"]["id"])
        elif args.source_id:
            result = call(ctx, content.read_source, args.source_id)
            if result["source"]["library_id"] != str(library_id):
                raise ValueError("request_refused")
            source_id = args.source_id
        else:
            raise ValueError("upload_original_in_browser_or_supply_url")
        return call(ctx, submit_processing, source_id, idempotency_key, extract=args.extract)

    @server.tool()
    def job_status(job_id: UUID, ctx: Context):
        return call(ctx, writes.job_status, JobStatusArgs(job_id=job_id).job_id)

    @server.tool()
    def export_skill(
        library_id: UUID,
        rule_ids: list[UUID],
        ctx: Context,
        install_to_device: Literal[False] = False,
    ):
        args = ExportSkillArgs(
            library_id=library_id, rule_ids=rule_ids, install_to_device=install_to_device
        )
        return call(ctx, content.export_skill, args.library_id, args.rule_ids)

    child = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[resource.rsplit("/mcp", 1)[0]],
        ),
    )
    return server, child
