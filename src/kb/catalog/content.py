"""Read-only catalog access for sources, knowledge, and exact rule versions.

Every database entry point binds the caller's database identity. PostgreSQL ACLs
and row-level security remain the sole authority for visibility.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, Principal, transaction_identity

__all__ = [
    "export_skill",
    "get_rule",
    "list_rules",
    "list_sources",
    "read_source",
    "search_knowledge",
]


def _require_principal(principal: Principal | None) -> None:
    if principal is None:
        raise AccessDenied("forbidden")


def _require_uuid(value: UUID | None, field: str) -> UUID:
    if value is None:
        raise AccessDenied("forbidden")
    if not isinstance(value, UUID):
        raise ValueError(f"{field} must be a UUID")
    return value


def _bounded_integer(value: int, field: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return value


def _normalise_uuids(
    values: Sequence[UUID] | None,
    field: str,
    *,
    deny_none: bool,
) -> tuple[UUID, ...] | None:
    if values is None:
        if deny_none:
            raise AccessDenied("forbidden")
        return None
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{field} must be a sequence of UUIDs")

    normalised: list[UUID] = []
    seen: set[UUID] = set()
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{field} must be a sequence of UUIDs") from exc

    for value in iterator:
        if value is None:
            if deny_none:
                raise AccessDenied("forbidden")
            raise ValueError(f"{field} cannot contain null")
        if not isinstance(value, UUID):
            raise ValueError(f"{field} must contain only UUIDs")
        if value not in seen:
            seen.add(value)
            normalised.append(value)
    return tuple(normalised)


def _load_actions(cursor: Any, rule_ids: Sequence[UUID]) -> dict[UUID, list[str]]:
    actions: dict[UUID, list[str]] = {rule_id: [] for rule_id in rule_ids}
    if not rule_ids:
        return actions

    cursor.execute(
        """
        SELECT a.rule_id, a.body
        FROM kb.rule_action AS a
        WHERE a.rule_id = ANY(%s::uuid[])
        ORDER BY a.rule_id, a.ordinal
        """,
        (list(rule_ids),),
    )
    for row in cursor.fetchall():
        actions[row["rule_id"]].append(row["body"])
    return actions


def _load_rule_provenance(
    cursor: Any, rule_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    provenance: dict[UUID, list[dict[str, Any]]] = {rule_id: [] for rule_id in rule_ids}
    if not rule_ids:
        return provenance

    cursor.execute(
        """
        SELECT rp.rule_id, rp.source_id, rp.fragment_id,
               f.id AS located_fragment_id,
               f.ordinal, f.locator_kind, f.file_page, f.printed_label,
               f.chapter, f.paragraph, f.snapshot_url, f.snapshot_at
        FROM kb.rule_provenance AS rp
        LEFT JOIN kb.fragment AS f
          ON f.id = rp.fragment_id
         AND f.source_id = rp.source_id
        WHERE rp.rule_id = ANY(%s::uuid[])
        ORDER BY rp.rule_id, rp.source_id, rp.fragment_id NULLS FIRST
        """,
        (list(rule_ids),),
    )
    for row in cursor.fetchall():
        locator = None
        if row["located_fragment_id"] is not None:
            locator = {
                "ordinal": row["ordinal"],
                "locator_kind": row["locator_kind"],
                "file_page": row["file_page"],
                "printed_label": row["printed_label"],
                "chapter": row["chapter"],
                "paragraph": row["paragraph"],
                "snapshot_url": row["snapshot_url"],
                "snapshot_at": row["snapshot_at"],
            }
        provenance[row["rule_id"]].append(
            {
                "source_id": row["source_id"],
                "fragment_id": row["fragment_id"],
                "locator": locator,
            }
        )
    return provenance


def _decorate_rules(cursor: Any, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rule_ids = [rule["id"] for rule in rules]
    actions = _load_actions(cursor, rule_ids)
    provenance = _load_rule_provenance(cursor, rule_ids)
    for rule in rules:
        rule_id = rule["id"]
        rule["actions"] = actions[rule_id]
        rule["provenance"] = provenance[rule_id]
    return rules


def _normalise_markdown_text(value: Any) -> str:
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _markdown_value(value: Any) -> str:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if value is None:
        return "null"
    if isinstance(value, str):
        return _normalise_markdown_text(value)
    if isinstance(value, (dict, list, tuple, int, float, bool)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return _normalise_markdown_text(value)


def _append_text_section(lines: list[str], heading: str, value: Any) -> None:
    if value is None or (isinstance(value, str) and not value.strip()):
        text = "Not specified."
    else:
        text = _markdown_value(value).rstrip()
    lines.extend([f"### {heading}", "", text, ""])


def _append_item_section(lines: list[str], heading: str, values: Any) -> None:
    if not values:
        lines.extend([f"### {heading}", "", "None recorded.", ""])
        return
    lines.extend([f"### {heading}", ""])
    for value in values:
        text = _markdown_value(value).replace("\n", "\n  ")
        lines.extend([f"- {text}"])
    lines.append("")


def _append_action_section(lines: list[str], actions: Sequence[str]) -> None:
    lines.extend(["### Actions", ""])
    if not actions:
        lines.extend(["None recorded.", ""])
        return
    for ordinal, action in enumerate(actions, start=1):
        text = _markdown_value(action).replace("\n", "\n   ")
        lines.append(f"{ordinal}. {text}")
    lines.append("")


def _locator_markdown(locator: dict[str, Any] | None) -> str:
    if locator is None:
        return "not available"
    # JSON quoting keeps source-controlled metadata on one inert display line.
    return (
        json.dumps(locator, ensure_ascii=False, default=str)
        .replace("`", "\\u0060")
        .replace("<", "\\u003c")
    )


def _build_skill_markdown(rules: Sequence[dict[str, Any]]) -> str:
    lines = ["# Portable Skill", ""]
    for position, rule in enumerate(rules, start=1):
        title = _normalise_markdown_text(rule["title"])
        lines.extend(
            [
                f"## {position}. {title}",
                "",
                f"- Rule ID: `{rule['id']}`",
                f"- Lineage ID: `{rule['lineage_id']}`",
                f"- Version: {rule['version_no']}",
                f"- Library ID: `{rule['library_id']}`",
                f"- Publication: {rule['publication']}",
                "",
            ]
        )
        _append_text_section(lines, "Applicability", rule["when_to_apply"])
        _append_item_section(lines, "Inputs", rule["inputs"])
        _append_item_section(lines, "Preconditions", rule["preconditions"])
        _append_action_section(lines, rule["actions"])
        _append_item_section(lines, "Exceptions", rule["exceptions"])
        _append_text_section(lines, "Expected effect", rule["expected_effect"])
        _append_text_section(lines, "Verification", rule["verification"])

        lines.extend(["### Provenance", ""])
        if not rule["provenance"]:
            lines.extend(["None recorded.", ""])
        else:
            for item in rule["provenance"]:
                fragment_id = item["fragment_id"]
                fragment_text = f"`{fragment_id}`" if fragment_id is not None else "not recorded"
                locator_text = (
                    _locator_markdown(item["locator"])
                    if fragment_id is not None
                    else "not recorded"
                )
                lines.append(
                    f"- Source ID: `{item['source_id']}`; "
                    f"Fragment ID: {fragment_text}; Locator: {locator_text}"
                )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def list_sources(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List RLS-visible sources in one library with their original byte size."""
    _require_principal(principal)
    library_id = _require_uuid(library_id, "library_id")
    limit = _bounded_integer(limit, "limit", minimum=1, maximum=100)
    offset = _bounded_integer(offset, "offset", minimum=0)

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT s.*, om.byte_size AS original_byte_size
            FROM kb.source AS s
            LEFT JOIN kb.object_manifest AS om ON om.object_key = s.object_key
            WHERE s.library_id = %s
            ORDER BY s.created_at DESC, s.id
            LIMIT %s OFFSET %s
            """,
            (library_id, limit, offset),
        )
        return [dict(row) for row in cursor.fetchall()]


def read_source(
    conn: psycopg.Connection,
    principal: Principal,
    source_id: UUID,
    from_fragment: int = 0,
    limit: int = 20,
) -> dict[str, Any]:
    """Read one visible source and a bounded, ordinal-ordered fragment page."""
    _require_principal(principal)
    source_id = _require_uuid(source_id, "source_id")
    from_fragment = _bounded_integer(from_fragment, "from_fragment", minimum=0)
    limit = _bounded_integer(limit, "limit", minimum=1, maximum=200)

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT s.*, om.byte_size AS original_byte_size
            FROM kb.source AS s
            LEFT JOIN kb.object_manifest AS om ON om.object_key = s.object_key
            WHERE s.id = %s
            """,
            (source_id,),
        )
        source_row = cursor.fetchone()
        if source_row is None:
            raise AccessDenied("forbidden")

        cursor.execute(
            """
            SELECT id, source_id, ordinal, locator_kind, file_page, printed_label,
                   chapter, paragraph, snapshot_url, snapshot_at,
                   table_index,cell_row,cell_column,text
            FROM kb.fragment
            WHERE source_id = %s
              AND ordinal >= %s
            ORDER BY ordinal
            LIMIT %s
            """,
            (source_id, from_fragment, limit),
        )
        fragments = [dict(row) for row in cursor.fetchall()]

    return {
        "source": dict(source_row),
        "fragments": fragments,
        "retrieved_only": True,
    }


def search_knowledge(
    conn: psycopg.Connection,
    principal: Principal,
    query: str,
    library_ids: Sequence[UUID] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Perform deterministic lexical retrieval over RLS-visible knowledge."""
    _require_principal(principal)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty")
    scoped_library_ids = _normalise_uuids(library_ids, "library_ids", deny_none=False)
    limit = _bounded_integer(limit, "limit", minimum=1, maximum=50)

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            WITH request AS (
                SELECT plainto_tsquery('simple', %s) AS tsquery
            )
            SELECT k.*,
                   ts_rank_cd(
                       to_tsvector('simple', k.statement),
                       request.tsquery
                   ) AS score
            FROM request
            CROSS JOIN kb.knowledge AS k
            WHERE request.tsquery IS NOT NULL
              AND to_tsvector('simple', k.statement) @@ request.tsquery
              AND (
                  %s::uuid[] IS NULL
                  OR k.library_id = ANY(%s::uuid[])
              )
            ORDER BY score DESC, k.created_at DESC, k.id
            LIMIT %s
            """,
            (
                query,
                list(scoped_library_ids) if scoped_library_ids is not None else None,
                list(scoped_library_ids) if scoped_library_ids is not None else None,
                limit,
            ),
        )
        hits = [dict(row) for row in cursor.fetchall()]

        provenance_by_hit: dict[UUID, list[dict[str, Any]]] = {hit["id"]: [] for hit in hits}
        if hits:
            cursor.execute(
                """
                SELECT kp.knowledge_id, kp.source_id, kp.fragment_id,
                       kp.author, kp.publication_ref, kp.url, kp.retrieved_at,
                       f.id AS located_fragment_id,
                       f.ordinal, f.locator_kind, f.file_page, f.printed_label,
                       f.chapter, f.paragraph, f.snapshot_url, f.snapshot_at
                FROM kb.knowledge_provenance AS kp
                LEFT JOIN kb.fragment AS f
                  ON f.id = kp.fragment_id
                 AND f.source_id = kp.source_id
                WHERE kp.knowledge_id = ANY(%s::uuid[])
                ORDER BY kp.knowledge_id, kp.source_id,
                         kp.fragment_id NULLS FIRST
                """,
                ([hit["id"] for hit in hits],),
            )
            for row in cursor.fetchall():
                locator = None
                if row["located_fragment_id"] is not None:
                    locator = {
                        "ordinal": row["ordinal"],
                        "locator_kind": row["locator_kind"],
                        "file_page": row["file_page"],
                        "printed_label": row["printed_label"],
                        "chapter": row["chapter"],
                        "paragraph": row["paragraph"],
                        "snapshot_url": row["snapshot_url"],
                        "snapshot_at": row["snapshot_at"],
                    }
                bucket = provenance_by_hit.get(row["knowledge_id"])
                if bucket is not None:
                    bucket.append(
                        {
                            "source_id": row["source_id"],
                            "fragment_id": row["fragment_id"],
                            "author": row["author"],
                            "publication_ref": row["publication_ref"],
                            "url": row["url"],
                            "retrieved_at": row["retrieved_at"],
                            "locator": locator,
                        }
                    )

    for hit in hits:
        hit["provenance"] = provenance_by_hit[hit["id"]]

    return {
        "mode": "lexical",
        "semantic_search_enabled": False,
        "hits": hits,
        "retrieved_only": True,
    }


def list_rules(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List the latest RLS-visible version of each rule lineage in a library."""
    _require_principal(principal)
    library_id = _require_uuid(library_id, "library_id")
    limit = _bounded_integer(limit, "limit", minimum=1, maximum=100)
    offset = _bounded_integer(offset, "offset", minimum=0)

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (r.lineage_id)
                   r.id, r.library_id, r.lineage_id, r.version_no, r.title,
                   r.when_to_apply, r.expected_effect, r.verification,
                   r.publication, r.inputs, r.preconditions, r.exceptions,
                   r.created_at
            FROM kb.rule AS r
            WHERE r.library_id = %s
            ORDER BY r.lineage_id, r.version_no DESC
            LIMIT %s OFFSET %s
            """,
            (library_id, limit, offset),
        )
        rules = [dict(row) for row in cursor.fetchall()]
        return _decorate_rules(cursor, rules)


def get_rule(
    conn: psycopg.Connection,
    principal: Principal,
    rule_id: UUID,
    version_no: int | None = None,
) -> dict[str, Any]:
    """Read an exact rule-version UUID, optionally guarded by its version number."""
    _require_principal(principal)
    rule_id = _require_uuid(rule_id, "rule_id")
    if version_no is not None:
        version_no = _bounded_integer(version_no, "version_no", minimum=1)

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT r.id, r.library_id, r.lineage_id, r.version_no, r.title,
                   r.when_to_apply, r.expected_effect, r.verification,
                   r.publication, r.inputs, r.preconditions, r.exceptions,
                   r.created_at
            FROM kb.rule AS r
            WHERE r.id = %s
              AND (
                  %s::integer IS NULL
                  OR r.version_no = %s::integer
              )
            """,
            (rule_id, version_no, version_no),
        )
        row = cursor.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        return _decorate_rules(cursor, [dict(row)])[0]


def export_skill(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    rule_ids: Sequence[UUID],
) -> dict[str, Any]:
    """Build a portable SKILL.md artifact without writing it to the server."""
    _require_principal(principal)
    library_id = _require_uuid(library_id, "library_id")
    requested_ids = _normalise_uuids(rule_ids, "rule_ids", deny_none=True)
    if requested_ids is None or not requested_ids:
        raise ValueError("rule_ids must contain at least one UUID")
    requested_ids = tuple(sorted(requested_ids, key=lambda value: value.bytes))

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT r.id, r.library_id, r.lineage_id, r.version_no, r.title,
                   r.when_to_apply, r.expected_effect, r.verification,
                   r.publication, r.inputs, r.preconditions, r.exceptions,
                   r.created_at
            FROM kb.rule AS r
            WHERE r.id = ANY(%s::uuid[])
              AND r.library_id = %s
              AND r.publication = 'published'
            ORDER BY r.id
            """,
            (list(requested_ids), library_id),
        )
        rules = [dict(row) for row in cursor.fetchall()]
        if len(rules) != len(requested_ids):
            raise AccessDenied("forbidden")
        rules = _decorate_rules(cursor, rules)

    markdown = _build_skill_markdown(rules)
    encoded = markdown.encode("utf-8")
    return {
        "filename": "SKILL.md",
        "content": markdown,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
    }
