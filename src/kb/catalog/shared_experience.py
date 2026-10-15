from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.retrieval_events import record_retrieval

_VISIBLE_COPY = """
    SELECT se.id, se.library_id, se.use_id, se.outcome_id, se.rule_id,
           se.rule_version_no, se.publisher_id, se.snapshot, se.published_at
    FROM kb.shared_experience AS se
    JOIN kb.library AS target ON target.id = se.library_id
    JOIN kb.rule AS r ON r.id = se.rule_id AND r.version_no = se.rule_version_no
    JOIN kb.library AS origin ON origin.id = r.library_id
    WHERE target.id = %s
      AND target.kind IN ('project', 'experience')
      AND target.organisation_id = origin.organisation_id
      AND kb.role_rank(kb.effective_role(kb.current_principal(), target.id)) >= 10
      AND kb.role_rank(kb.effective_role(kb.current_principal(), origin.id)) >= 10
      AND r.publication = 'published'
"""


def _content_hash(snapshot: Any) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _publish_preflight(
    cur, principal: Principal, library_id: UUID, use_id: UUID, outcome_id: UUID
) -> None:
    if principal.generation_watermark < 1:
        raise AccessDenied("forbidden")
    cur.execute(
        """
        SELECT kb.experience_snapshot(%s, %s) AS snapshot
        FROM kb.library AS target
        JOIN kb.use_record AS u ON u.id = %s
        JOIN kb.outcome AS o ON o.id = %s AND o.use_id = u.id
        JOIN kb.rule AS r ON r.id = u.rule_id AND r.version_no = u.rule_version_no
        JOIN kb.library AS origin ON origin.id = r.library_id
        WHERE target.id = %s
          AND target.kind IN ('project', 'experience')
          AND target.organisation_id = origin.organisation_id
          AND target.organisation_id = nullif(current_setting('app.account', true), '')::uuid
          AND r.publication = 'published'
          AND u.principal_id = kb.current_principal()
          AND o.reported_by = kb.current_principal()
          AND u.state <> 'retrieved'
          AND kb.role_rank(kb.effective_role(kb.current_principal(), target.id)) >= 30
        """,
        (use_id, outcome_id, use_id, outcome_id, library_id),
    )
    row = cur.fetchone()
    if row is None or row["snapshot"] is None:
        raise AccessDenied("forbidden")


def publish(
    conn, principal: Principal, library_id: UUID, use_id: UUID, outcome_id: UUID
) -> dict[str, Any]:
    """Publish the caller's canonical applied/outcomed use and outcome."""
    with transaction_identity(conn, principal):
        with conn.cursor(row_factory=dict_row) as cur:
            # This check deliberately precedes the unique-key lookup. A replay
            # is therefore still denied after a grant or origin publication is
            # revoked.
            _publish_preflight(cur, principal, library_id, use_id, outcome_id)

            cur.execute(
                """
                INSERT INTO kb.shared_experience
                    (library_id, use_id, outcome_id, rule_id, rule_version_no,
                     publisher_id, snapshot)
                SELECT %s, %s, %s,
                       (snapshot->>'rule_id')::uuid,
                       (snapshot->>'rule_version_no')::integer,
                       kb.current_principal(), snapshot
                FROM (SELECT kb.experience_snapshot(%s, %s) AS snapshot) AS canonical
                WHERE canonical.snapshot IS NOT NULL
                ON CONFLICT (library_id, use_id, outcome_id) DO NOTHING
                """,
                (library_id, use_id, outcome_id, use_id, outcome_id),
            )

            # Do not interpret a permission error as a replay. This SELECT is
            # reachable only after the complete preflight succeeded.
            cur.execute(
                _VISIBLE_COPY
                + " AND se.library_id = %s AND se.use_id = %s AND se.outcome_id = %s"
                + " ORDER BY se.published_at DESC, se.id DESC LIMIT 1",
                (library_id, library_id, use_id, outcome_id),
            )
            row = cur.fetchone()
            if row is None:
                raise AccessDenied("forbidden")
            result = dict(row)
            result["content_hash"] = _content_hash(result["snapshot"])
            return result


def _aggregate_rows(cur, library_id: UUID) -> list[dict[str, Any]]:
    cur.execute(
        sql.SQL("""WITH visible AS ({visible})
        , latest AS (
            SELECT DISTINCT ON (use_id) rule_id,rule_version_no,snapshot
            FROM visible
            ORDER BY use_id,(snapshot->>'reported_at')::timestamptz DESC,
                     published_at DESC,id DESC
        )
        SELECT rule_id,rule_version_no,count(*)::integer AS total,
               count(snapshot->>'rating')::integer AS rated,
               avg((snapshot->>'rating')::double precision) AS average_rating,
               jsonb_build_object(
                   'met',count(*) FILTER(WHERE snapshot->>'achievement'='met'),
                   'partly',count(*) FILTER(WHERE snapshot->>'achievement'='partly'),
                   'not_met',count(*) FILTER(WHERE snapshot->>'achievement'='not_met'),
                   'unknown',count(*) FILTER(
                       WHERE coalesce(snapshot->>'achievement','unknown')='unknown')
               ) AS achievement_counts,
               jsonb_build_object(
                   'human',count(*) FILTER(WHERE snapshot->>'assessor_kind'='human'),
                   'agent',count(*) FILTER(WHERE snapshot->>'assessor_kind'='agent'),
                   'measurement',count(*) FILTER(WHERE snapshot->>'assessor_kind'='measurement'),
                   'unknown',count(*) FILTER(
                       WHERE coalesce(snapshot->>'assessor_kind','unknown')='unknown')
               ) AS assessor_kind_counts
        FROM latest GROUP BY rule_id,rule_version_no
        ORDER BY rule_id,rule_version_no LIMIT 100
        """).format(visible=sql.SQL(_VISIBLE_COPY)),
        (library_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def list_shared(conn, principal: Principal, library_id: UUID) -> dict[str, Any]:
    if principal.generation_watermark < 1:
        raise AccessDenied("forbidden")
    with transaction_identity(conn, principal):
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _VISIBLE_COPY + " ORDER BY se.published_at DESC, se.id DESC LIMIT 100",
                (library_id,),
            )
            items: list[dict[str, Any]] = []
            for row in cur.fetchall():
                item = dict(row)
                item["content_hash"] = _content_hash(item["snapshot"])
                item["retrieval_event_id"] = record_retrieval(
                    cur, principal, item_kind="experience", item_id=item["id"]
                )
                items.append(item)
            by_rule_version = _aggregate_rows(cur, library_id)
            return {
                "items": items,
                "by_rule_version": by_rule_version,
                "basis": "explicit_shared_self_report_not_business_proof",
            }
