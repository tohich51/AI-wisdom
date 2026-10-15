"""MCP adapter uses the trusted transport; SQL curation remains in writes.publish."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from kb.mcp.server import build_mcp


async def test_publication_rechecks_transport_identity(monkeypatch):
    monkeypatch.setenv("KB_MCP_RESOURCE_URL", "https://gateway.example/mcp")
    monkeypatch.setenv("KB_OIDC_ISSUER", "https://id.example/realms/kb")
    monkeypatch.setenv("KB_MCP_CLIENT_ID", "fixture")
    principal_id, account_id, knowledge_id = uuid4(), uuid4(), uuid4()
    bearer = str(uuid4())
    conn, calls, resolutions = object(), [], []

    def resolve(token):
        assert token == bearer
        resolutions.append(token)
        return SimpleNamespace(principal_id=principal_id, account_id=account_id)

    def publish(db, principal, ident, *, knowledge=False):
        assert db is conn
        assert principal.principal_id == principal_id and principal.account_id == account_id
        assert ident == knowledge_id and knowledge is True
        calls.append(ident)
        return {"id": str(ident), "publication": "published"}

    app = SimpleNamespace(
        state=SimpleNamespace(
            resolve_token=resolve,
            db_pool=SimpleNamespace(connection=lambda: nullcontext(conn)),
        )
    )
    monkeypatch.setattr("kb.catalog.writes.publish", publish)
    server, _ = build_mcp(app, ["gateway.example"])
    tools = await server.list_tools()
    assert "publish_knowledge" in {tool.name for tool in tools}
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"authorization": "Bearer " + bearer})
        )
    )
    result = await server.call_tool(
        "publish_knowledge", {"knowledge_id": str(knowledge_id)}, context=context
    )
    assert not result.is_error and len(calls) == 1 and len(resolutions) == 1

    def revoked(_token):
        raise HTTPException(status_code=403)

    app.state.resolve_token = revoked
    with pytest.raises(UnexpectedToolError):
        await server.call_tool(
            "publish_knowledge", {"knowledge_id": str(knowledge_id)}, context=context
        )
    assert len(calls) == 1
