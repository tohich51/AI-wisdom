"""Synthetic unit tests for the indexed-evidence compact graph payload.

Every fixture here is invented for these tests (``.invalid``-style synthetic
sources, invented quotes). They assert wire-format/identity properties of the
pure helper and the strict arg model. They can prove nothing about live catalog
permissions, provider access or citation policy; those gates are unchanged and
remain owned by the catalog and integration suites.
"""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from kb.contracts.research_tools import SemanticArgs
from kb.mcp.graph_payload import (
    EVIDENCE_FORMAT,
    EVIDENCE_FORMAT_KEY,
    EVIDENCE_INDEX_KEY,
    EVIDENCE_REFS_KEY,
    EvidenceFormatCollision,
    compact_network,
)

LONG_QUOTE = (
    "Synthetic quotation invented for kb tests — naïve façade, über-citation "
    'synthesis, 引用文献, 🧪 emoji payload, "quoted" and \\escaped\\ text. '
) * 12

LIB = "11111111-2222-3333-4444-555555555555"


def _row(locator: str, **extra: object) -> dict:
    base = {
        "quote": LONG_QUOTE,
        "locator": locator,
        "source_version": "v1.4.2",
        "offsets": [10, 2048],
        "hash": "sha256:" + ("a1b2c3d4" * 8),
        "metadata": {"scorer": 0.87, "tags": ["synthetic", "ünïcode"]},
    }
    base.update(extra)
    return base


def _payload() -> dict:
    shared = _row("§12.3 p. 91")
    other_locator = _row("§12.4 p. 92")
    same_quote_new_version = _row("§12.3 p. 91", source_version="v2.0.0", offsets=[12, 2048])
    return {
        "library_id": LIB,
        "truncated": False,
        "capabilities": {"similarity": True, "max_nodes": 500},
        "counts": {"nodes": 3, "edges": 2},
        "nodes": [
            {
                "id": "n0",
                "label": "Synthetic node ✦",
                "body": "Body text with émoji 🧬 and «angles».",
                "publication": {"title": "Synthetic paper", "year": 2024},
                "origin": "ingest",
                "version": 3,
                "evidence": [shared, other_locator, shared],
            },
            {
                "id": "n1",
                "label": "Node without evidence",
                "body": "…",
                "version": 1,
                "similarity": {"score": 0.42, "neighbors": ["n0", "n2"]},
                "truncated": False,
                "capabilities": {"similarity": True},
            },
            {
                "id": "n2",
                "label": "Node with empty evidence",
                "version": 2,
                "evidence": [],
                "similarity": {"score": 1.0},
            },
        ],
        "edges": [
            {
                "source": "n0",
                "target": "n2",
                "relation": "supports",
                "evidence": [same_quote_new_version],
            },
            {
                "source": "n1",
                "target": "n2",
                "relation": "cites",
                "evidence": [shared, other_locator],
            },
        ],
    }


def _expand(compact: dict) -> dict:
    """Inverse of the wire format: expand refs via the index into evidence."""
    index = compact[EVIDENCE_INDEX_KEY]
    out = {
        key: value
        for key, value in compact.items()
        if key not in (EVIDENCE_FORMAT_KEY, EVIDENCE_INDEX_KEY)
    }
    for field in ("nodes", "edges"):
        items = []
        for item in out[field]:
            if isinstance(item, dict) and EVIDENCE_REFS_KEY in item:
                item = dict(item)
                item["evidence"] = [index[ref] for ref in item.pop(EVIDENCE_REFS_KEY)]
            items.append(item)
        out[field] = items
    return out


def _dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def test_round_trip_is_exact_and_input_is_never_mutated():
    original = _payload()
    before = copy.deepcopy(original)

    compact = compact_network(original)

    # Input untouched, top-level fields preserved in order, only two added.
    assert original == before
    assert list(compact) == [*before, EVIDENCE_FORMAT_KEY, EVIDENCE_INDEX_KEY]
    assert compact[EVIDENCE_FORMAT_KEY] == EVIDENCE_FORMAT

    # refs are key strings, never row dicts; order and multiplicity preserved.
    refs = compact["nodes"][0][EVIDENCE_REFS_KEY]
    assert [type(ref) for ref in refs] == [str, str, str]
    assert refs[0] == refs[2] and refs[0] != refs[1]
    assert list(compact[EVIDENCE_INDEX_KEY]) == ["e0", "e1", "e2"]
    assert compact["edges"][0][EVIDENCE_REFS_KEY] == ["e2"]

    # Identity is the full canonical row: same quote, different locator /
    # version / offset must stay three distinct index entries, metadata intact.
    index = compact[EVIDENCE_INDEX_KEY]
    quoted = {ref: row for ref, row in index.items() if row["quote"] == LONG_QUOTE}
    assert len(quoted) == 3
    assert {(r["locator"], r["source_version"], tuple(r["offsets"])) for r in quoted.values()} == {
        ("§12.3 p. 91", "v1.4.2", (10, 2048)),
        ("§12.4 p. 92", "v1.4.2", (10, 2048)),
        ("§12.3 p. 91", "v2.0.0", (12, 2048)),
    }
    # Absent stays absent; empty evidence becomes empty refs.
    assert "evidence" not in compact["nodes"][1]
    assert EVIDENCE_REFS_KEY not in compact["nodes"][1]
    assert "evidence" not in compact["nodes"][2]
    assert compact["nodes"][2][EVIDENCE_REFS_KEY] == []

    # Expanding refs through the index reproduces the whole payload exactly,
    # every top-level field, node/edge sibling and metadata value included.
    assert _expand(compact) == before


def test_reserved_keys_and_non_list_evidence_fail_closed():
    empty = compact_network({"nodes": [{"id": "a"}, {"id": "b", "evidence": []}], "edges": []})
    assert EVIDENCE_REFS_KEY not in empty["nodes"][0]
    assert empty["nodes"][1][EVIDENCE_REFS_KEY] == []
    assert empty[EVIDENCE_INDEX_KEY] == {}

    row = _row("§1.1")
    for reserved in (EVIDENCE_FORMAT_KEY, EVIDENCE_INDEX_KEY, EVIDENCE_REFS_KEY):
        with pytest.raises(EvidenceFormatCollision):
            compact_network({reserved: "x", "nodes": [], "edges": []})
        # Even with no evidence at all: the reserved name is refused up front.
        with pytest.raises(EvidenceFormatCollision):
            compact_network({"nodes": [{"id": "a", reserved: "x"}], "edges": []})
        with pytest.raises(EvidenceFormatCollision):
            compact_network({"nodes": [], "edges": [{"source": "a", "target": "b", reserved: "x"}]})
        with pytest.raises(EvidenceFormatCollision):
            compact_network({"nodes": [{"id": "a", reserved: "x", "evidence": [row]}]})

    with pytest.raises(EvidenceFormatCollision):
        compact_network({"nodes": [{"evidence": {"quote": "not a list"}}]})
    with pytest.raises(TypeError):
        compact_network(["not", "a", "dict"])


def test_repeated_rows_shrink_and_compact_args_stay_strict():
    row = _row("§12.3 p. 91")
    expanded = {
        "truncated": False,
        "nodes": [
            {"id": f"n{i}", "label": "Synthetic ✦", "evidence": [row, row, row, row]}
            for i in range(60)
        ],
        "edges": [{"source": "n0", "target": "n1", "relation": "supports", "evidence": [row]}],
    }

    compact = compact_network(expanded)

    assert list(compact[EVIDENCE_INDEX_KEY]) == ["e0"]
    assert all(
        refs == ["e0"] * 4 for refs in (item[EVIDENCE_REFS_KEY] for item in compact["nodes"])
    )
    assert len(_dumps(compact)) < len(_dumps(expanded))
    assert _expand(compact) == expanded

    assert SemanticArgs(library_id=LIB).compact is False
    assert SemanticArgs(library_id=LIB, compact=True).compact is True
    # Strict bool: no string parsing and no int/bool coercion.
    for bad in ("true", 1, "True", [1]):
        with pytest.raises(ValidationError):
            SemanticArgs(library_id=LIB, compact=bad)
