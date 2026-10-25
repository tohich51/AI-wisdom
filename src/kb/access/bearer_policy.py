"""Action scopes and trusted client classification, after JWT verification."""

import os

from kb.access.policy import AccessDenied, Principal
from kb.contracts.operations import (
    ALIAS_PREFIX,
    CANONICAL_PREFIX,
    canonical_api_path,
    operation_for,
)

HANDLER_SCOPES = {
    "approve_browser_login": "kb:read",
    "list_libraries": "kb:read",
    "search_knowledge": "kb:read",
    "read_source": "kb:read",
    "get_rule": "kb:read",
    "project_context": "kb:read",
    "job_status": "kb:read",
    "list_jobs": "kb:read",
    "get_policy": "kb:read",
    "experience_summary": "kb:read",
    "list_retrievals": "kb:read",
    "list_research_sources": "kb:read",
    "list_research": "kb:read",
    "get_research": "kb:read",
    "list_notes": "kb:read",
    "get_note": "kb:read",
    "provenance_graph": "kb:read",
    "semantic_network": "kb:read",
    "enqueue_similarity": "kb:contribute",
    "publish": "kb:contribute",
    "review_batch": "kb:contribute",
    "edit_knowledge": "kb:contribute",
    "submit_url": "kb:contribute",
    "upload_source_file": "kb:contribute",
    "submit_processing": "kb:contribute",
    "submit_research": "kb:contribute",
    "save_note": "kb:contribute",
    "draft_rule_from_research": "kb:contribute",
    "create_concept": "kb:contribute",
    "create_relation": "kb:contribute",
    "review_semantic": "kb:contribute",
    "start_use": "kb:experience",
    "mark_applied": "kb:experience",
    "record_outcome": "kb:experience",
    "publish_experience": "kb:experience",
    "list_shared": "kb:read",
    "export_skill": "kb:export",
    "export_research": "kb:export",
}


def require_scope(identity, scope: str | None) -> None:
    if scope is None or scope not in identity.scopes:
        raise AccessDenied("forbidden")


def bearer_principal(identity) -> Principal:
    # Locality is administrator configuration bound to signed azp. No request
    # argument, IP address, header or caller assertion can mark a client local.
    local = set(os.environ.get("KB_MCP_LOCAL_CLIENT_IDS", "").split())
    client = getattr(identity, "client_id", None)
    return Principal(
        identity.principal_id,
        identity.account_id,
        cloud_client=not (isinstance(client, str) and client in local),
    )


def api_scope_path(path: str) -> str:
    """Collapse the canonical prefix back onto the shipped /api form.

    The rules in :func:`http_scope` are frozen and are written against a literal
    ``/api`` prefix. Normalising first means adding ``/api/v1`` cannot quietly
    drop the ``kb:admin`` checks that match on ``/api/libraries/`` and let a
    grants or policy route answer under a weaker scope. A path outside the API
    prefixes is returned untouched, so the auth and static routes keep the
    behaviour they always had.
    """
    canonical = canonical_api_path(path)
    if canonical is None:
        return path
    return f"{ALIAS_PREFIX}{canonical[len(CANONICAL_PREFIX) :]}"


def http_scope(method: str, path: str) -> str:
    operation = operation_for(method, path)
    if operation is not None:
        return operation.scope
    path = api_scope_path(path)
    if "/orgs/" in path:
        return "kb:admin"  # Administrative MCP is intentionally absent in v1.
    if method not in {"GET", "HEAD"} and path.startswith("/api/libraries/"):
        if "/grants" in path or path.endswith("/policy"):
            return "kb:admin"
    if path.endswith(("/export", "/original", "/markdown")):
        return "kb:export"
    if method in {"GET", "HEAD"}:
        return "kb:read"
    if (
        path == "/api/uses"
        or path.startswith("/api/uses/")
        or (path.startswith("/api/libraries/") and path.endswith("/experience"))
    ):
        return "kb:experience"
    return "kb:contribute"


def scope_for_request(method: str, path: str) -> str | None:
    """The scope a request must carry, taken from its registered operation.

    The registry is the single declaration of what an operation is and what it
    needs. Dispatch reads it here, so the two API prefixes, the router and the
    MCP surface cannot drift apart: there is one answer per (method, path), not
    one per surface plus a cross-check that a test has to remember to run.

    ``None`` means closed. A request that lands on a path under the protected
    API prefixes which no operation claims gets ``None``, and
    :func:`require_scope` denies it. That is the default the contract asks for,
    and it is what makes a newly added, undeclared route fail closed instead of
    inheriting a scope from a pattern match on its URL.

    A path outside the API prefixes is not a product operation, so it keeps the
    historical pattern answer: ``/auth/*``, ``/mcp``, ``/healthz`` and
    ``/static`` are not governed by the operations table, and
    :func:`http_scope` is the only rule that ever applied to them.
    """
    if canonical_api_path(path) is None:
        return http_scope(method, path)
    operation = operation_for(method, path)
    if operation is None:
        return None
    return operation.scope
