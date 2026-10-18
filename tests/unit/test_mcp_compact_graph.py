import json
from uuid import UUID

import pytest
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp.types import CallToolResult, TextContent
from pydantic_core import to_json

from kb.mcp.research import install_tools
from kb.research import semantic

LIBRARY_ID = UUID("12345678-1234-5678-1234-567812345678")
CANONICAL_CITATION_COUNT = 153


class FakeServer:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


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


def test_get_semantic_network_propagates_request_refusal() -> None:
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
        semantic_network(library_id=LIBRARY_ID, ctx=object())

    assert error.value is refusal
