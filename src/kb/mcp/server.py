"""Official MCP transport; catalog calls share authenticated PostgreSQL identity."""

import os
from typing import Literal
from uuid import UUID

import anyio
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent
from pydantic import AnyHttpUrl, ValidationError

from kb.access.bearer_policy import HANDLER_SCOPES, bearer_principal, require_scope
from kb.catalog import (
    content,
    libraries,
    policies,
    projects,
    shared_experience,
    upload,
    workspace,
    writes,
)
from kb.contracts.enums import LibraryKind
from kb.contracts.mcp_tools import (
    MCP_TOOLS,
    EditKnowledgeArgs,
    ExperienceSummaryArgs,
    ExportSkillArgs,
    GetProjectContextArgs,
    GetRuleArgs,
    JobStatusArgs,
    ListJobsArgs,
    ListLibrariesArgs,
    PublishExperienceArgs,
    PublishKnowledgeArgs,
    ReadLibraryPolicyArgs,
    ReadSourceArgs,
    RecordOutcomeArgs,
    ReviewItemsArgs,
    SearchKnowledgeArgs,
    SharedExperienceArgs,
    StartUseArgs,
    SubmitSourceArgs,
)
from kb.jobs.ingestion import submit_processing


def assert_mcp_contract_registered() -> None:
    """Every MCP tool in the frozen contract declares an operation.

    A new tool that forgot to register would otherwise ship with no HTTP
    binding and no declared scope, and nothing would notice until someone
    compared the two surfaces by hand.
    """
    from kb.contracts.mcp_tools import EXPECTED_TOOLS
    from kb.contracts.operations import MCP_OPERATIONS, assert_registry_consistent

    assert_registry_consistent()
    missing = sorted(EXPECTED_TOOLS - set(MCP_OPERATIONS))
    if missing:
        raise ValueError(f"MCP tools without a registry operation: {missing}")
    unknown = sorted(set(MCP_OPERATIONS) - EXPECTED_TOOLS)
    if unknown:
        raise ValueError(f"the registry binds tools the contract does not declare: {unknown}")


def build_mcp(app, hosts):
    assert_mcp_contract_registered()
    resource = os.environ["KB_MCP_RESOURCE_URL"]

    class Verifier:
        async def verify_token(self, token):
            try:
                identity = await anyio.to_thread.run_sync(app.state.resolve_token, token)
                return AccessToken(
                    token=token,
                    client_id=identity.client_id or os.environ["KB_MCP_CLIENT_ID"],
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
        try:
            require_scope(identity, HANDLER_SCOPES.get(handler.__name__))
            principal = bearer_principal(identity)
            with app.state.db_pool.connection() as conn:
                return jsonable_encoder(handler(conn, principal, *args, **kwargs))
        except Exception:
            raise ValueError("request_refused") from None

    from kb.mcp.research import install_tools

    install_tools(server, call)

    @server.tool()
    def list_jobs(ctx: Context, library_id: UUID | None = None):
        args = ListJobsArgs(library_id=library_id)
        return call(ctx, workspace.list_jobs, args.library_id)

    @server.tool()
    def read_library_policy(library_id: UUID, ctx: Context):
        args = ReadLibraryPolicyArgs(library_id=library_id)
        return call(ctx, policies.get_policy, args.library_id)

    @server.tool()
    def get_experience_summary(ctx: Context):
        ExperienceSummaryArgs()
        return call(ctx, workspace.experience_summary)

    @server.tool()
    def list_retrievals(ctx: Context):
        """Own bounded delivery journal; delivery is not reading or application."""
        ExperienceSummaryArgs()
        return {"events": call(ctx, workspace.list_retrievals)}

    @server.tool()
    def publish_experience(library_id: UUID, use_id: UUID, outcome_id: UUID, ctx: Context):
        """Publish one's exact outcome copy to an authorized project/experience library."""
        args = PublishExperienceArgs(library_id=library_id, use_id=use_id, outcome_id=outcome_id)

        def publish_experience(conn, principal, *values):
            return shared_experience.publish(conn, principal, *values)

        return call(ctx, publish_experience, args.library_id, args.use_id, args.outcome_id)

    @server.tool()
    def list_shared_experience(library_id: UUID, ctx: Context):
        """Read explicit shared snapshots within both destination and source permissions."""
        args = SharedExperienceArgs(library_id=library_id)
        return call(ctx, shared_experience.list_shared, args.library_id)

    @server.tool()
    def review_items(
        library_id: UUID,
        item_kind: Literal["knowledge", "rule"],
        item_ids: list[UUID],
        decision: Literal["publish", "reject"],
        ctx: Context,
    ):
        args = ReviewItemsArgs(
            library_id=library_id, item_kind=item_kind, item_ids=item_ids, decision=decision
        )
        body = workspace.ReviewBatch(
            item_kind=args.item_kind,
            item_ids=[str(value) for value in args.item_ids],
            decision=args.decision,
        )
        return call(ctx, workspace.review_batch, args.library_id, body)

    @server.tool()
    def edit_knowledge(
        knowledge_id: UUID,
        statement: str,
        expected_statement: str,
        kind: Literal["assertion", "definition", "recommendation", "example"],
        ctx: Context,
    ):
        args = EditKnowledgeArgs(
            knowledge_id=knowledge_id,
            statement=statement,
            expected_statement=expected_statement,
            kind=kind,
        )
        body = workspace.EditKnowledge(
            statement=args.statement, expected_statement=args.expected_statement, kind=args.kind
        )
        return call(ctx, workspace.edit_knowledge, args.knowledge_id, body)

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
        source_ids: list[UUID] | None = None,
    ):
        args = SearchKnowledgeArgs(
            query=query,
            library_ids=library_ids,
            limit=limit,
            use_generation=use_generation,
            source_ids=source_ids,
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
            source_ids=args.source_ids,
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
        idempotency_key: str | None = None,
    ):
        args = StartUseArgs(
            rule_id=rule_id,
            version_no=version_no,
            note=note,
            expected_result=expected_result,
            where_applied=where_applied,
            idempotency_key=idempotency_key,
        )
        result = call(
            ctx,
            writes.start_use,
            args.rule_id,
            args.version_no,
            args.expected_result,
            args.where_applied,
            idempotency_key=args.idempotency_key,
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
        idempotency_key: str | None = None,
        achievement: Literal["met", "partly", "not_met", "unknown"] | None = None,
        assessor_kind: Literal["human", "agent", "measurement", "unknown"] = "unknown",
        assessment_evidence: str | None = None,
    ):
        args = RecordOutcomeArgs(
            use_id=use_id,
            observed_result=observed_result,
            rating=rating,
            rating_basis=rating_basis,
            is_correction=is_correction,
            confirm_applied=confirm_applied,
            idempotency_key=idempotency_key,
            achievement=achievement,
            assessor_kind=assessor_kind,
            assessment_evidence=assessment_evidence,
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
            idempotency_key=args.idempotency_key,
            achievement=args.achievement,
            assessor_kind=args.assessor_kind,
            assessment_evidence=args.assessment_evidence,
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
    # Supported action scopes are advertised without requiring every client
    # to hold all of them. The actual tool class is checked on each call.
    child.routes[:0] = create_protected_resource_routes(
        resource_url=AnyHttpUrl(resource),
        authorization_servers=[AnyHttpUrl(os.environ["KB_OIDC_ISSUER"])],
        scopes_supported=[
            "openid",
            "profile",
            "email",
            "kb:read",
            "kb:contribute",
            "kb:experience",
            "kb:export",
        ],
    )
    return server, child
