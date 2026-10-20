"""Read-only catalog access for sources, knowledge, and exact rule versions.

Every database entry point binds the caller's database identity. PostgreSQL ACLs
and row-level security remain the sole authority for visibility.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.policies import require_export
from kb.catalog.retrieval_events import (
    record_knowledge_retrievals,
    record_retrieval,
)
from kb.runtime_identity import identity_connection
from kb.vendor.book_to_skill.validate_skill import audit as audit_skill

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
               s.content_hash, s.author, s.publication_ref, s.origin_url AS url, s.retrieved_at,
               f.id AS located_fragment_id,
               f.ordinal, f.locator_kind, f.file_page, f.printed_label,
               f.chapter, f.paragraph, f.snapshot_url, f.snapshot_at,
               f.table_index, f.cell_row, f.cell_column
        FROM kb.rule_provenance AS rp
        JOIN kb.source AS s ON s.id = rp.source_id
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
                "table_index": row["table_index"],
                "cell_row": row["cell_row"],
                "cell_column": row["cell_column"],
            }
        provenance[row["rule_id"]].append(
            {
                "source_id": row["source_id"],
                "fragment_id": row["fragment_id"],
                "content_hash": row["content_hash"],
                "author": row["author"],
                "publication_ref": row["publication_ref"],
                "url": row["url"],
                "retrieved_at": row["retrieved_at"],
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
    *,
    fragment_id: UUID | None = None,
) -> dict[str, Any]:
    """Read one visible source and a bounded, ordinal-ordered fragment page.

    When the authorized source carries a stored ``origin_url``, ``url_history``
    reports the nearest earlier and later source in the same library that shares
    that exact stored URL, ordered by (created_at, id). No
    canonicalization or renumbering is applied. For local files (no
    ``origin_url``) the ``url_history`` key is omitted from the result entirely.

    When ``fragment_id`` is supplied, source authorization is checked first and
    the target must be a visible fragment of that source. ``fragment_id`` takes
    precedence over ``from_fragment``; missing, inaccessible, or mismatched
    targets are refused without falling back to ``from_fragment``.
    """
    _require_principal(principal)
    source_id = _require_uuid(source_id, "source_id")
    from_fragment = _bounded_integer(from_fragment, "from_fragment", minimum=0)
    limit = _bounded_integer(limit, "limit", minimum=1, maximum=200)
    if fragment_id is not None:
        fragment_id = _require_uuid(fragment_id, "fragment_id")

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

        url_history = None
        if source_row["origin_url"]:
            cursor.execute(
                """
                SELECT id, title, content_hash, retrieved_at, created_at
                FROM kb.source
                WHERE library_id = %s
                  AND origin_url = %s
                  AND (created_at, id) < (%s, %s)
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (
                    source_row["library_id"],
                    source_row["origin_url"],
                    source_row["created_at"],
                    source_id,
                ),
            )
            previous_row = cursor.fetchone()
            cursor.execute(
                """
                SELECT id, title, content_hash, retrieved_at, created_at
                FROM kb.source
                WHERE library_id = %s
                  AND origin_url = %s
                  AND (created_at, id) > (%s, %s)
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (
                    source_row["library_id"],
                    source_row["origin_url"],
                    source_row["created_at"],
                    source_id,
                ),
            )
            next_row = cursor.fetchone()
            url_history = {
                "origin_url": source_row["origin_url"],
                "previous": dict(previous_row) if previous_row else None,
                "next": dict(next_row) if next_row else None,
            }

        resolved_from_fragment = from_fragment
        if fragment_id is not None:
            cursor.execute(
                """
                SELECT ordinal
                FROM kb.fragment
                WHERE id = %s
                  AND source_id = %s
                LIMIT 1
                """,
                (fragment_id, source_id),
            )
            target_row = cursor.fetchone()
            if target_row is None:
                raise AccessDenied("forbidden")
            resolved_from_fragment = target_row["ordinal"]

        cursor.execute(
            """
            SELECT id, source_id, ordinal, locator_kind, file_page, printed_label,
                   chapter, paragraph, snapshot_url, snapshot_at,
                   table_index,cell_row,cell_column,text,
                   parser_name,parser_version,spine,heading
            FROM kb.fragment
            WHERE source_id = %s
              AND ordinal >= %s
            ORDER BY ordinal
            LIMIT %s
            """,
            (source_id, resolved_from_fragment, limit),
        )
        fragments = [dict(row) for row in cursor.fetchall()]
        event_id = record_retrieval(cursor, principal, item_kind="source", item_id=source_id)

    result = {
        "source": dict(source_row),
        "fragments": fragments,
        "retrieved_only": True,
        "retrieval_event_id": event_id,
    }
    if url_history is not None:
        result["url_history"] = url_history
    return result


def attach_knowledge_provenance(cursor, hits) -> None:
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


def lexical_search_knowledge(
    conn: psycopg.Connection,
    principal: Principal,
    query: str,
    library_ids: Sequence[UUID] | None = None,
    limit: int = 10,
    *,
    source_ids: Sequence[UUID] | None = None,
) -> dict[str, Any]:
    """Perform deterministic lexical retrieval over RLS-visible knowledge."""
    _require_principal(principal)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty")
    scoped_library_ids = _normalise_uuids(library_ids, "library_ids", deny_none=False)
    sources = _normalise_uuids(source_ids, "source_ids", deny_none=False)
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
              AND k.publication <> 'withdrawn'
              AND NOT EXISTS(SELECT 1 FROM kb.knowledge_provenance p
                             JOIN kb.source s ON s.id=p.source_id
                             WHERE p.knowledge_id=k.id AND s.publication='withdrawn')
              AND to_tsvector('simple', k.statement) @@ request.tsquery
              AND (
                  %s::uuid[] IS NULL
                  OR k.library_id = ANY(%s::uuid[])
              )
              AND (%s::uuid[] IS NULL OR EXISTS (
                  SELECT 1 FROM kb.knowledge_provenance p JOIN kb.source s ON s.id=p.source_id
                  WHERE p.knowledge_id=k.id AND s.id=ANY(%s::uuid[])
              ))
            ORDER BY score DESC, k.created_at DESC, k.id
            LIMIT %s
            """,
            (
                query,
                list(scoped_library_ids) if scoped_library_ids is not None else None,
                list(scoped_library_ids) if scoped_library_ids is not None else None,
                list(sources) if sources is not None else None,
                list(sources) if sources is not None else None,
                limit,
            ),
        )
        hits = [dict(row) for row in cursor.fetchall()]

        attach_knowledge_provenance(cursor, hits)

    return {
        "mode": "lexical",
        "semantic_search_enabled": False,
        "hits": hits,
        "retrieved_only": True,
    }


def search_knowledge(
    conn,
    principal,
    query,
    library_ids=None,
    limit=10,
    *,
    semantic=None,
    source_ids=None,
    connections=None,
    deadline=None,
):
    """Shared HTTP/MCP search; the configured service owns semantic retrieval.

    One entrypoint, called identically by the HTTP route and the MCP tool.

    ``conn`` is the connection the caller already holds, or ``None`` when it
    holds none. Passing ``connections`` -- the shared factory -- is how a
    transport says "do not keep a lease for this": the semantic service then
    takes its own short SQL phases and holds nothing while the provider is
    being called, and the retrieval audit below opens its own short phase
    rather than extending a lease the transport is still holding.
    """
    _require_principal(principal)
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("query must contain 1..2000 characters")
    # One place decides which factory this search uses: the caller's, or the
    # one the configured semantic service was built with. The audit phase below
    # needs the same answer, so it is resolved once rather than twice.
    factory = connections
    if factory is None and semantic is not None:
        factory = getattr(semantic, "connections", None)
    if conn is None and factory is None:
        raise ValueError("a connection or a connection factory is required")
    scopes = _normalise_uuids(library_ids, "library_ids", deny_none=False)
    sources = _normalise_uuids(source_ids, "source_ids", deny_none=False)
    limit = _bounded_integer(limit, "limit", minimum=1, maximum=50)
    if semantic is not None:
        result = semantic.search(
            conn,
            principal,
            query,
            scopes,
            limit,
            source_ids=sources,
            connections=connections,
            deadline=deadline,
        )
    else:
        if conn is None:
            with identity_connection(factory, principal) as lexical_conn:
                result = lexical_search_knowledge(
                    lexical_conn, principal, query, scopes, limit, source_ids=sources
                )
        else:
            result = lexical_search_knowledge(
                conn, principal, query, scopes, limit, source_ids=sources
            )
    # The audit is the last thing that happens and it is its own short phase.
    if conn is not None:
        with (
            transaction_identity(conn, principal),
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            result["retrieval_event_ids"] = record_knowledge_retrievals(
                cursor,
                principal,
                item_ids=tuple(UUID(str(hit["id"])) for hit in result["hits"]),
            )
    else:
        with identity_connection(factory, principal) as audit_conn:
            with audit_conn.cursor(row_factory=dict_row) as cursor:
                result["retrieval_event_ids"] = record_knowledge_retrievals(
                    cursor,
                    principal,
                    item_ids=tuple(UUID(str(hit["id"])) for hit in result["hits"]),
                )
    return result


def list_rules(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List RLS-visible rule versions, keeping published history beside draft corrections."""
    _require_principal(principal)
    library_id = _require_uuid(library_id, "library_id")
    limit = _bounded_integer(limit, "limit", minimum=1, maximum=100)
    offset = _bounded_integer(offset, "offset", minimum=0)

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            """
            SELECT r.id, r.library_id, r.lineage_id, r.version_no, r.title,
                   r.when_to_apply, r.expected_effect, r.verification,
                   r.publication, r.inputs, r.preconditions, r.exceptions,
                   r.created_at
            FROM kb.rule AS r
            WHERE r.library_id = %s
            ORDER BY r.created_at DESC, r.lineage_id, r.version_no DESC, r.id
            LIMIT %s OFFSET %s
            """,
            (library_id, limit, offset),
        )
        rules = [dict(row) for row in cursor.fetchall()]
        for rule in rules:
            record_retrieval(
                cursor,
                principal,
                item_kind="rule",
                item_id=rule["id"],
                version_no=rule["version_no"],
            )
        return _decorate_rules(cursor, rules)


def get_rule(
    conn: psycopg.Connection,
    principal: Principal,
    rule_id: UUID,
    version_no: int | None = None,
    *,
    _record_delivery: bool = True,
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
        result = _decorate_rules(cursor, [dict(row)])[0]
        if _record_delivery:
            result["retrieval_event_id"] = record_retrieval(
                cursor,
                principal,
                item_kind="rule",
                item_id=rule_id,
                version_no=result["version_no"],
            )
        return result


def _build_skill_archive(markdown: str, manifest_json: str, manifest_sha256: str) -> dict[str, Any]:
    """Pack the deliverable into a deterministic in-memory ZIP.

    Two entries only, in fixed order, with a pinned timestamp so the same
    export always hashes identically. Nothing is written to the server and no
    original book bytes are included; the archive is never installed or run.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for entry_name, payload in (("SKILL.md", markdown), ("manifest.json", manifest_json)):
            info = zipfile.ZipInfo(entry_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o644 << 16
            archive.writestr(info, payload.encode("utf-8"))
    blob = buffer.getvalue()
    return {
        "filename": f"ai-wisdom-{manifest_sha256[:16]}.zip",
        "content_base64": base64.b64encode(blob).decode("ascii"),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob),
    }


def export_skill(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    rule_ids: Sequence[UUID],
) -> dict[str, Any]:
    """Build a portable SKILL.md artifact and ZIP archive without writing it to the server."""
    _require_principal(principal)
    library_id = _require_uuid(library_id, "library_id")
    requested_ids = _normalise_uuids(rule_ids, "rule_ids", deny_none=True)
    if requested_ids is None or not requested_ids:
        raise ValueError("rule_ids must contain at least one UUID")
    requested_ids = tuple(sorted(requested_ids, key=lambda value: value.bytes))

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cursor:
        require_export(cursor, [library_id])
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
        cursor.execute(
            "SELECT DISTINCT s.id,s.library_id,s.content_hash,s.title,s.author,s.edition "
            "FROM kb.rule_provenance p JOIN kb.source s ON s.id=p.source_id "
            "WHERE p.rule_id=ANY(%s::uuid[]) ORDER BY s.id",
            (list(requested_ids),),
        )
        sources = [dict(row) for row in cursor.fetchall()]
        require_export(cursor, [source["library_id"] for source in sources])

    markdown = _build_skill_markdown(rules)
    markdown = (
        f"---\nname: ai-wisdom-{library_id}\n"
        "description: Проверенные правила библиотеки.\n---\n\n" + markdown
    )
    # Reuse the pinned upstream validator; generated content alone enters the
    # private temporary directory. No installation or agent permission is granted.
    with tempfile.TemporaryDirectory(prefix="kb-skill-") as directory:
        skill_path = Path(directory) / "SKILL.md"
        skill_path.write_text(markdown, encoding="utf-8")
        for lens in ("hermes", "opencode", "claude"):
            errors, _warnings = audit_skill(skill_path, lens=lens)
            if errors:
                raise ValueError("portable_skill_validation_failed")
    encoded = markdown.encode("utf-8")
    manifest = {
        "schema_version": 1,
        "library_id": str(library_id),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "rules": [
            {"id": str(r["id"]), "lineage_id": str(r["lineage_id"]), "version_no": r["version_no"]}
            for r in rules
        ],
        "sources": [
            {key: str(value) if isinstance(value, UUID) else value for key, value in s.items()}
            for s in sources
        ],
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest_sha256 = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    # Built before the closing recheck, from already-validated text only.
    archive = _build_skill_archive(markdown, manifest_json, manifest_sha256)
    # Validation and rendering may take time. Recheck the complete export closure
    # immediately before delivery; do not record failed or revoked exports.
    with transaction_identity(conn, principal), conn.cursor() as cursor:
        require_export(cursor, [library_id, *[source["library_id"] for source in sources]])
        for rule in rules:
            record_retrieval(
                cursor,
                principal,
                item_kind="rule",
                item_id=rule["id"],
                version_no=rule["version_no"],
            )
    return {
        "filename": "SKILL.md",
        "content": markdown,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "manifest": manifest,
        "manifest_sha256": manifest_sha256,
        "archive": archive,
    }
