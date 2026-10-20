"""Losslessly index repeated citation objects after canonical graph checks."""

from __future__ import annotations

import json
from typing import Any

EVIDENCE_FORMAT_KEY = "evidence_format"
EVIDENCE_INDEX_KEY = "evidence_index"
EVIDENCE_REFS_KEY = "evidence_refs"
EVIDENCE_FORMAT = "indexed-v1"


class EvidenceFormatCollision(ValueError):
    """Raised when a payload already uses reserved compact-format keys."""


def compact_network(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` in the indexed-evidence compact format."""
    if not isinstance(data, dict):
        raise TypeError("compact_network expects a dict payload")
    out = dict(data)
    index: dict[str, Any] = {}  # slot -> evidence row, in first-seen order
    slots: dict[str, str] = {}  # canonical row -> slot
    for reserved in (EVIDENCE_FORMAT_KEY, EVIDENCE_INDEX_KEY, EVIDENCE_REFS_KEY):
        if reserved in out:
            raise EvidenceFormatCollision(f"reserved key {reserved!r} already present at root")
    for field in ("nodes", "edges"):
        items = out.get(field)
        if not isinstance(items, list):
            continue
        compacted: list[Any] = []
        for position, item in enumerate(items):
            if not isinstance(item, dict):
                compacted.append(item)
                continue
            for reserved in (EVIDENCE_FORMAT_KEY, EVIDENCE_INDEX_KEY, EVIDENCE_REFS_KEY):
                if reserved in item:
                    raise EvidenceFormatCollision(
                        f"reserved key {reserved!r} already present at {field}[{position}]"
                    )
            if "evidence" not in item:
                compacted.append(item)
                continue
            rows = item["evidence"]
            if not isinstance(rows, list):
                raise EvidenceFormatCollision(f"{field}[{position}].evidence is not a list")
            refs: list[str] = []
            for row in rows:
                identity = json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False)
                if identity not in slots:
                    slots[identity] = f"e{len(index)}"
                    index[slots[identity]] = row
                refs.append(slots[identity])  # the key string, never the row
            entry = dict(item)  # shallow copy; the caller's object is untouched
            del entry["evidence"]
            entry[EVIDENCE_REFS_KEY] = refs
            compacted.append(entry)
        out[field] = compacted
    out[EVIDENCE_FORMAT_KEY] = EVIDENCE_FORMAT
    out[EVIDENCE_INDEX_KEY] = index
    return out
