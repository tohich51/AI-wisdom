import asyncio
import json
from uuid import UUID

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic_core import to_json

from kb.mcp.research import install_tools
from kb.research import semantic

LIBRARY_ID = UUID("12345678-1234-5678-1234-567812345678")
CANONICAL_CITATION_COUNT = 153


class FakeServer:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.tool_annotations: dict[str, ToolAnnotations | None] = {}

    def tool(self, *, annotations: ToolAnnotations | None = None):
        def register(function):
            self.tools[function.__name__] = function
            self.tool_annotations[function.__name__] = annotations
            return function

        return register


def test_sdk_catalog_exposes_truthful_semantic_read_annotations() -> None:
    server = MCPServer("compact-graph-catalog")

    def shared_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("catalog listing must not execute a tool")

    install_tools(server, shared_call)
    catalog = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    semantic_tool = catalog["get_semantic_network"]

    assert semantic_tool.annotations is not None
    assert semantic_tool.annotations.model_dump(mode="json", by_alias=True, exclude_none=True) == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert semantic_tool.input_schema == {
        "properties": {
            "library_id": {"format": "uuid", "title": "Library Id", "type": "string"},
            "limit": {"default": 200, "title": "Limit", "type": "integer"},
            "compact": {"default": False, "title": "Compact", "type": "boolean"},
        },
        "required": ["library_id"],
        "title": "get_semantic_networkArguments",
        "type": "object",
    }
    assert semantic_tool.input_schema["properties"]["limit"]["default"] == 200
    assert semantic_tool.output_schema is None

    for tool_name, catalog_tool in catalog.items():
        if tool_name == "get_semantic_network":
            continue
        annotations = catalog_tool.annotations
        annotation_json = (
            annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
            if annotations is not None
            else {}
        )
        assert annotation_json.get("readOnlyHint") is not True


def _synthetic_graph(library_id: UUID, limit: int) -> dict[str, object]:
    citations = [
        {
            "id": f"canonical-{index:03d}",
            "canonical": True,
            "evidence": {
                "text": f"“Canonical evidence {index}: ∑ ≤ ∞ 🧬”",
                "offsets": [4096 + index * 17, 4144 + index * 17],
                "hash": f"{index:064x}",
            },
        }
        for index in range(CANONICAL_CITATION_COUNT)
    ]
    return {
        "kind": "semantic_network",
        "version": 1,
        "library_id": str(library_id),
        "limit": limit,
        "nodes": [
            {
                "id": "concept-root",
                "label": "“Root concept” — primary",
                "score": 0.125,
                "citation_ids": ["canonical-152", "canonical-076", "canonical-000"],
            },
            {
                "id": "concept-child",
                "label": "“Child concept” — derived",
                "score": 0.3333333333333333,
                "citation_ids": ["canonical-001", "canonical-077", "canonical-151"],
            },
        ],
        "edges": [
            {
                "id": "relation-000",
                "from_id": "concept-child",
                "to_id": "concept-root",
                "predicate": "supports",
                "weight": -0.5,
                "citation_ids": ["canonical-152", "canonical-076", "canonical-000"],
            }
        ],
        "citations": citations,
    }


@pytest.mark.parametrize(
    ("limit", "expected_limit"),
    [(None, 200), (37, 37)],
    ids=("default-limit", "custom-limit"),
)
def test_get_semantic_network_compacts_complete_sdk_result(
    limit: int | None,
    expected_limit: int,
) -> None:
    server = FakeServer()
    ctx = object()
    fixture = _synthetic_graph(LIBRARY_ID, expected_limit)
    calls: list[tuple[object, object, UUID, int]] = []

    def shared_call(
        ctx_arg: object,
        handler: object,
        library_id: UUID,
        received_limit: int,
    ) -> dict[str, object]:
        calls.append((ctx_arg, handler, library_id, received_limit))
        return fixture

    install_tools(server, shared_call)
    semantic_network = server.tools["get_semantic_network"]

    if limit is None:
        result = semantic_network(library_id=LIBRARY_ID, ctx=ctx)
    else:
        result = semantic_network(library_id=LIBRARY_ID, ctx=ctx, limit=limit)

    assert len(calls) == 1
    called_ctx, called_handler, called_library_id, called_limit = calls[0]
    assert called_ctx is ctx
    assert called_handler is semantic.semantic_network
    assert called_library_id == LIBRARY_ID
    assert called_limit == expected_limit

    metadata = func_metadata(semantic_network, skip_names=["ctx"])
    converted = metadata.convert_result(result)

    assert converted is result
    assert isinstance(converted, CallToolResult)
    assert converted.structured_content is None
    assert len(converted.content) == 1
    text_content = converted.content[0]
    assert isinstance(text_content, TextContent)

    decoded = json.loads(text_content.text)
    assert decoded == fixture
    assert decoded["limit"] == expected_limit
    assert decoded["nodes"][0]["score"] == 0.125
    assert decoded["edges"][0]["weight"] == -0.5
    assert decoded["edges"][0]["citation_ids"] == [
        "canonical-152",
        "canonical-076",
        "canonical-000",
    ]

    decoded_citations = decoded["citations"]
    assert len(decoded_citations) == CANONICAL_CITATION_COUNT
    assert decoded_citations == fixture["citations"]
    assert all(citation["canonical"] for citation in decoded_citations)
    assert [citation["id"] for citation in decoded_citations] == [
        f"canonical-{index:03d}" for index in range(CANONICAL_CITATION_COUNT)
    ]
    assert decoded_citations[0]["evidence"]["offsets"] == [4096, 4144]
    assert decoded_citations[-1]["evidence"]["hash"] == f"{CANONICAL_CITATION_COUNT - 1:064x}"
    assert "“Canonical evidence 0: ∑ ≤ ∞ 🧬”" in text_content.text
    assert r"\u201c" not in text_content.text

    compact_wire = text_content.text.encode("utf-8")
    sdk_pretty_wire = to_json(fixture, fallback=str, indent=2)
    assert len(compact_wire) < len(sdk_pretty_wire)


@pytest.mark.parametrize("compact", [False, True])
def test_get_semantic_network_propagates_request_refusal(compact: bool) -> None:
    server = FakeServer()
    refusal = ValueError("request_refused")

    def shared_call(
        ctx: object,
        handler: object,
        library_id: UUID,
        limit: int,
    ) -> None:
        raise refusal

    install_tools(server, shared_call)
    semantic_network = server.tools["get_semantic_network"]

    with pytest.raises(ValueError, match=r"\Arequest_refused\Z") as error:
        semantic_network(library_id=LIBRARY_ID, ctx=object(), compact=compact)

    assert error.value is refusal


def test_opt_in_indexes_checked_citations_through_registered_handler() -> None:
    server = FakeServer()
    row = {"quote": "Synthetic quote", "locator": {"file_page": 7}, "source_version_id": "v2"}
    fixture = {"nodes": [{"id": "n1", "evidence": [row, row]}], "edges": [], "truncated": False}
    calls = []

    def shared_call(ctx, handler, library_id, limit):
        calls.append((ctx, handler, library_id, limit))
        return fixture

    install_tools(server, shared_call)
    ctx = object()
    result = server.tools["get_semantic_network"](library_id=LIBRARY_ID, ctx=ctx, compact=True)
    assert calls == [(ctx, semantic.semantic_network, LIBRARY_ID, 200)]
    decoded = json.loads(result.content[0].text)
    assert decoded == {
        "nodes": [{"id": "n1", "evidence_refs": ["e0", "e0"]}],
        "edges": [],
        "truncated": False,
        "evidence_format": "indexed-v1",
        "evidence_index": {"e0": row},
    }
    assert fixture["nodes"][0]["evidence"] == [row, row]


@pytest.mark.parametrize("compact", [False, True])
def test_wire_header_is_opt_in_after_admission(compact):
    from types import SimpleNamespace

    from kb.mcp.graph_wire import WIRE_VERSION, decode_text

    server = FakeServer()
    fixture = _synthetic_graph(LIBRARY_ID, 200)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"x-kb-mcp-wire": WIRE_VERSION})
        )
    )
    admitted = []

    def shared_call(*args):
        admitted.append(args)
        return fixture

    install_tools(server, shared_call)
    result = server.tools["get_semantic_network"](library_id=LIBRARY_ID, ctx=ctx, compact=compact)
    assert len(admitted) == 1
    text = result.content[0].text
    if compact:
        assert "_kb_wire" in json.loads(text)
        from kb.mcp.graph_payload import compact_network

        assert json.loads(decode_text(text)) == compact_network(fixture)
    else:
        assert json.loads(text) == fixture
