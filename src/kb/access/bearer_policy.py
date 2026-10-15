"""Action scopes and trusted client classification, after JWT verification."""

import os

from kb.access.policy import AccessDenied, Principal

HANDLER_SCOPES = {
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
    "publish": "kb:contribute",
    "review_batch": "kb:contribute",
    "edit_knowledge": "kb:contribute",
    "submit_url": "kb:contribute",
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


def http_scope(method: str, path: str) -> str:
    if "/orgs/" in path:
        return "kb:admin"  # Administrative MCP is intentionally absent in v1.
    if method not in {"GET", "HEAD"} and path.startswith("/api/libraries/"):
        if "/grants" in path or path.endswith("/policy"):
            return "kb:admin"
    if path.endswith(("/export", "/original", "/markdown")):
        return "kb:export"
    if method in {"GET", "HEAD"}:
        return "kb:read"
    if path == "/api/uses" or path.startswith("/api/uses/"):
        return "kb:experience"
    return "kb:contribute"
