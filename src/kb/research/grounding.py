"""Immutable selected-source snapshots and exact canonical citation resolution.

Offsets count Unicode code points. Metadata and locators come from PostgreSQL;
every dependency is rechecked before any derived content is returned.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, Principal, transaction_identity


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def check_snapshot(cur: Any, manifest: dict) -> None:
    from psycopg.types.json import Jsonb

    cur.execute("SELECT kb.can_read_manifest(%s) AS allowed", (Jsonb(manifest),))
    row = cur.fetchone()
    allowed = row["allowed"] if isinstance(row, dict) else row[0]
    if allowed is not True:
        raise AccessDenied("forbidden")


def _excerpt(text: str, query: str, size: int) -> tuple[int, str]:
    tokens = re.findall(r"\w{3,}", query.casefold())[:30]
    # Case folding can change string length; locate against the original with
    # regex IGNORECASE so canonical code-point offsets are preserved.
    positions = [m.start() for word in tokens if (m := re.search(re.escape(word), text, re.I))]
    start = max(0, min(positions, default=0) - 300)
    return start, text[start : start + size]


def capture_snapshot(
    conn: Any,
    principal: Principal,
    source_ids: list[UUID],
    query: str = "",
    max_fragments: int = 24,
    budget: int = 18000,
) -> dict:
    if not 1 <= len(source_ids) <= 10 or len(set(source_ids)) != len(source_ids):
        raise ValueError("select_distinct_sources")
    if not 1 <= max_fragments <= 24 or not 2000 <= budget <= 20000:
        raise ValueError("invalid_context_limit")
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT s.id,s.library_id,s.title,s.author,s.edition,s.publication_ref,
              s.content_hash,s.processing,s.origin_url,s.retrieved_at,
              v.id AS source_version_id,v.version_no
              FROM kb.source s JOIN kb.library l ON l.id=s.library_id
              JOIN LATERAL (SELECT id,version_no FROM kb.source_version
                 WHERE source_id=s.id AND content_hash=s.content_hash
                 ORDER BY version_no DESC LIMIT 1) v ON true
              WHERE s.id=ANY(%s::uuid[]) AND s.publication<>'withdrawn'
                AND l.organisation_id=%s ORDER BY s.id""",
            (source_ids, principal.account_id),
        )
        sources = cur.fetchall()
        if len(sources) != len(source_ids):
            raise AccessDenied("forbidden")
        if any(row["processing"] not in ("done", "partial") for row in sources):
            raise ValueError("sources_not_ready")
        quota = max_fragments // len(source_ids)
        groups, total = [], 0
        for ident in source_ids:
            cur.execute(
                "SELECT count(*) AS n FROM kb.fragment WHERE source_id=%s "
                "AND length(btrim(text))>0",
                (ident,),
            )
            count = cur.fetchone()["n"]
            if not count:
                raise ValueError("sources_not_ready")
            total += count
            if query.strip():
                cur.execute(
                    """SELECT f.*, ts_rank_cd(to_tsvector('simple',f.text),
                      websearch_to_tsquery('simple',%s)) AS rank FROM kb.fragment f
                      WHERE f.source_id=%s AND length(btrim(f.text))>0
                      ORDER BY rank DESC,ordinal LIMIT %s""",
                    (query, ident, quota),
                )
            else:
                # Studio samples across the whole source, including its first
                # and last excerpt, rather than summarising only opening pages.
                cur.execute(
                    """WITH ordered AS (SELECT f.*,row_number() OVER(ORDER BY ordinal) AS rn
                       FROM kb.fragment f WHERE source_id=%s AND length(btrim(text))>0)
                       SELECT DISTINCT ON (rn) ordered.* FROM ordered
                       JOIN generate_series(0,%s-1) pos ON rn=1+round(
                          pos::numeric*(%s-1)/greatest(1,%s-1))::bigint ORDER BY rn""",
                    (ident, quota, count, quota),
                )
            groups.append(cur.fetchall())
        # Round-robin source coverage is bounded before passing any model text.
        ordered = [group[i] for i in range(quota) for group in groups if i < len(group)]
        fragments, pinned, remaining = [], [], budget
        clipped = False
        for index, row in enumerate(ordered[:max_fragments]):
            reserve = max(0, len(source_ids) - index - 1) * 100
            size = min(1800, budget // len(ordered), remaining - reserve)
            if size <= 0:
                break
            start, text = _excerpt(row["text"], query, size)
            clipped = clipped or start > 0 or start + len(text) < len(row["text"])
            digest = _hash(row["text"])
            entry = {
                "fragment_id": str(row["id"]),
                "source_id": str(row["source_id"]),
                "text_hash": digest,
                "excerpt_start": start,
                "excerpt_end": start + len(text),
            }
            pinned.append(entry)
            fragments.append(
                {
                    "id": entry["fragment_id"],
                    "source_id": entry["source_id"],
                    "text": text,
                    "start_offset": start,
                    "text_hash": digest,
                }
            )
            remaining -= len(text)
        manifest = {
            "sources": [
                {
                    "source_id": str(row["id"]),
                    "source_version_id": str(row["source_version_id"]),
                    "content_hash": row["content_hash"],
                }
                for row in sources
            ],
            "fragments": pinned,
        }
        check_snapshot(cur, manifest)
        metadata = [{**row, "partial": row["processing"] == "partial"} for row in sources]
        return {
            "manifest": manifest,
            "fragments": fragments,
            "sources": metadata,
            "context_limited": total > len(fragments) or clipped,
        }


def _canonical_unchecked(cur: Any, manifest: dict, fragment_id: UUID) -> tuple[dict, dict, dict]:
    pin = next((v for v in manifest["fragments"] if v["fragment_id"] == str(fragment_id)), None)
    if pin is None:
        raise ValueError("invalid_citation")
    source = next(v for v in manifest["sources"] if v["source_id"] == pin["source_id"])
    cur.execute(
        """SELECT f.*,s.title,s.author,s.edition,s.publication_ref,s.content_hash,
          v.id AS source_version_id FROM kb.fragment f JOIN kb.source s ON s.id=f.source_id
          JOIN kb.source_version v ON v.source_id=s.id AND v.id=%s
          WHERE f.id=%s AND s.id=%s AND s.publication<>'withdrawn'""",
        (UUID(source["source_version_id"]), fragment_id, UUID(source["source_id"])),
    )
    row = cur.fetchone()
    if row is None or _hash(row["text"]) != pin["text_hash"]:
        raise AccessDenied("forbidden")
    return row, pin, source


def _canonical(cur: Any, manifest: dict, fragment_id: UUID) -> tuple[dict, dict, dict]:
    check_snapshot(cur, manifest)
    return _canonical_unchecked(cur, manifest, fragment_id)


def _citation(row: dict, pin: dict, source: dict, start: int, end: int) -> dict:
    identity = f"{source['source_version_id']}:{row['id']}:{start}:{end}:{pin['text_hash']}"
    locator_fields = (
        "locator_kind",
        "file_page",
        "printed_label",
        "chapter",
        "paragraph",
        "snapshot_url",
        "snapshot_at",
        "table_index",
        "cell_row",
        "cell_column",
        "ordinal",
    )
    return {
        "id": _hash(identity),
        "source_id": pin["source_id"],
        "source_version_id": source["source_version_id"],
        "content_hash": source["content_hash"],
        "fragment_id": str(row["id"]),
        "text_hash": pin["text_hash"],
        "start": start,
        "end": end,
        "quote": row["text"][start:end],
        "title": row["title"],
        "author": row["author"],
        "edition": row["edition"],
        "publication_ref": row["publication_ref"],
        "locator": {key: row.get(key) for key in locator_fields},
    }


def resolve_citation(cur: Any, manifest: dict, fragment_id: UUID, quote: str) -> dict:
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
        raise ValueError("invalid_citation")
    row, pin, source = _canonical(cur, manifest, fragment_id)
    excerpt = row["text"][pin["excerpt_start"] : pin["excerpt_end"]]
    offset = excerpt.find(quote)
    if offset < 0:
        raise ValueError("invalid_citation")
    start = pin["excerpt_start"] + offset
    return _citation(row, pin, source, start, start + len(quote))


def _validate_citation_fields(citation: dict, row: dict, pin: dict, source: dict) -> dict:
    start, end = citation["start"], citation["end"]
    if type(start) is not int or type(end) is not int:
        raise ValueError("invalid_citation")
    if not pin["excerpt_start"] <= start < end <= pin["excerpt_end"]:
        raise ValueError("invalid_citation")
    canonical = _citation(row, pin, source, start, end)
    for key in (
        "id",
        "source_id",
        "source_version_id",
        "content_hash",
        "fragment_id",
        "text_hash",
        "start",
        "end",
        "quote",
    ):
        if citation.get(key) != canonical[key]:
            raise ValueError("invalid_citation")
    return canonical


def validate_citation(cur: Any, manifest: dict, citation: dict) -> dict:
    try:
        row, pin, source = _canonical(cur, manifest, UUID(citation["fragment_id"]))
        return _validate_citation_fields(citation, row, pin, source)
    except (KeyError, TypeError, StopIteration) as exc:
        raise ValueError("invalid_citation") from exc


def validate_citations(cur: Any, manifest: dict, citations: list[dict]) -> list[dict]:
    validated = []
    for citation in citations:
        try:
            row, pin, source = _canonical_unchecked(cur, manifest, UUID(citation["fragment_id"]))
            validated.append(_validate_citation_fields(citation, row, pin, source))
        except (KeyError, TypeError, StopIteration) as exc:
            raise ValueError("invalid_citation") from exc
    check_snapshot(cur, manifest)
    return validated
