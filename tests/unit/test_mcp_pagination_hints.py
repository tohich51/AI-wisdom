"""MCP pagination discovery and truthful annotations for the original ten tools."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from pydantic import ValidationError

from kb.catalog.libraries import LibraryKind
from kb.contracts.mcp_tools import ListLibrariesArgs
from kb.mcp.server import build_mcp

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_UNKEYED_APPEND = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

EXPECTED_ANNOTATIONS = {
    "list_libraries": _READ_ONLY,
    "search_knowledge": {**_READ_ONLY, "openWorldHint": True},
    "read_source": _READ_ONLY,
    "get_rule": _READ_ONLY,
    "get_project_context": _READ_ONLY,
    "start_use": _UNKEYED_APPEND,
    "record_outcome": _UNKEYED_APPEND,
    "submit_source": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "job_status": _READ_ONLY,
    "export_skill": _UNKEYED_APPEND,
}


def _identity(scopes):
    return SimpleNamespace(
        principal_id=uuid4(),
        account_id=uuid4(),
        scopes=frozenset(scopes),
        client_id="fixture",
    )


def _set_mcp_env(monkeypatch):
    monkeypatch.setenv("KB_MCP_RESOURCE_URL", "https://mcp.example.test/mcp")
    monkeypatch.setenv("KB_OIDC_ISSUER", "https://issuer.example.test")
    monkeypatch.setenv("KB_MCP_CLIENT_ID", "fixture")


def _app(identity, conn):
    return SimpleNamespace(
        state=SimpleNamespace(
            resolve_token=lambda _token: identity,
            db_connection=lambda: nullcontext(conn),
        )
    )


async def test_discovery_exposes_pagination_and_only_ten_truthful_annotations(monkeypatch):
    _set_mcp_env(monkeypatch)
    identity = _identity({"kb:read"})
    server, _ = build_mcp(_app(identity, object()), ["mcp.example.test"])
    tools = {tool.name: tool for tool in await server.list_tools()}

    list_schema = tools["list_libraries"].input_schema
    limit_schema = list_schema["properties"]["limit"]
    offset_schema = list_schema["properties"]["offset"]
    assert limit_schema["type"] == "integer"
    assert limit_schema["default"] == 50
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 200
    assert offset_schema["type"] == "integer"
    assert offset_schema["default"] == 0
    assert offset_schema["minimum"] == 0
    assert "limit" not in list_schema.get("required", [])
    assert "offset" not in list_schema.get("required", [])

    annotated_names = {name for name, tool in tools.items() if tool.annotations is not None}
    assert set(EXPECTED_ANNOTATIONS) <= annotated_names
    emitted = {
        name: tools[name].model_dump(mode="json", by_alias=True, exclude_none=True)["annotations"]
        for name in EXPECTED_ANNOTATIONS
    }
    assert emitted == EXPECTED_ANNOTATIONS
    assert "stable idempotency_key" in tools["start_use"].description
    assert "stable idempotency_key" in tools["record_outcome"].description


@pytest.mark.parametrize(
    ("field", "value"),
    (("limit", 0), ("limit", 201), ("offset", -1)),
)
def test_list_libraries_contract_enforces_pagination_bounds(field, value):
    with pytest.raises(ValidationError):
        ListLibrariesArgs.model_validate({field: value})


def test_list_libraries_contract_rejects_identity_injection():
    for field in ("user_id", "actor_id", "account_id", "role", "principal_id"):
        with pytest.raises(ValidationError):
            ListLibrariesArgs.model_validate({field: str(uuid4())})


async def test_list_libraries_forwards_page_through_trusted_scope_and_middleware(monkeypatch):
    _set_mcp_env(monkeypatch)
    identity = _identity({"kb:read"})
    conn = object()
    calls = []
    resolutions = []

    def resolve(token):
        resolutions.append(token)
        return identity

    def list_libraries(db, principal, *, kinds, limit, offset):
        assert db is conn
        assert principal.principal_id == identity.principal_id
        assert principal.account_id == identity.account_id
        calls.append((kinds, limit, offset))
        return {"libraries": [], "total": 0, "limit": limit, "offset": offset}

    monkeypatch.setattr("kb.catalog.libraries.list_libraries", list_libraries)
    app = SimpleNamespace(
        state=SimpleNamespace(
            resolve_token=resolve,
            db_connection=lambda: nullcontext(conn),
        )
    )
    server, _ = build_mcp(app, ["mcp.example.test"])
    bearer = str(uuid4())
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"authorization": f"Bearer {bearer}"})
        )
    )

    result = await server.call_tool(
        "list_libraries",
        {"include_kinds": ["reference"], "limit": 7, "offset": 11},
        context=context,
    )
    assert not result.is_error
    assert calls == [([LibraryKind.REFERENCE], 7, 11)]
    assert resolutions == [bearer]

    strict_arguments = next(
        middleware
        for middleware in server.middleware
        if getattr(middleware, "__name__", None) == "strict_arguments"
    )
    continued = False

    async def call_next(_ctx):
        nonlocal continued
        continued = True
        return None

    refused = await strict_arguments(
        SimpleNamespace(
            method="tools/call",
            params={
                "name": "list_libraries",
                "arguments": {"user_id": str(uuid4())},
            },
        ),
        call_next,
    )
    assert refused.is_error
    assert refused.content[0].text == "invalid_tool_arguments"
    assert not continued
    assert calls == [([LibraryKind.REFERENCE], 7, 11)]

    identity.scopes = frozenset()
    with pytest.raises(UnexpectedToolError):
        await server.call_tool(
            "list_libraries",
            {"limit": 7, "offset": 11},
            context=context,
        )
    assert calls == [([LibraryKind.REFERENCE], 7, 11)]
    assert resolutions == [bearer, bearer]
