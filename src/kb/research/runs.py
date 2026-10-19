"""Private queued Research/Studio operations; HTTP and MCP share this catalog."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

import procrastinate
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.admission import check_admission
from kb.jobs.provider_admission import PROVIDER_QUEUE_LOCK
from kb.research.grounding import capture_snapshot, check_snapshot, validate_citation

KINDS = frozenset(
    {"research", "summary", "faq", "comparison", "checklist", "flashcards", "topic", "network"}
)


def _library(cur: Any, principal: Principal, library_id: UUID, generation: bool = False) -> dict:
    cur.execute(
        """SELECT kb.role_rank(kb.effective_role(%s,l.id)) AS rank,
          kb.is_org_admin(%s,%s) AS admin FROM kb.library l
          WHERE l.id=%s AND l.organisation_id=%s""",
        (
            principal.principal_id,
            principal.account_id,
            principal.principal_id,
            library_id,
            principal.account_id,
        ),
    )
    row = cur.fetchone()
    if row is None or row["rank"] is None or row["rank"] < (20 if generation else 10):
        raise AccessDenied("forbidden")
    if generation and row["admin"] is not True:
        raise AccessDenied("forbidden")
    return row


def list_research_sources(conn: Any, principal: Principal, library_id: UUID) -> dict:
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        access = _library(cur, principal, library_id)
        cur.execute(
            """SELECT s.id,s.title,s.author,s.edition,s.processing,s.publication,
              s.processing='partial' AS partial,
              s.processing IN ('done','partial') AND EXISTS(SELECT 1 FROM kb.fragment f
                 WHERE f.source_id=s.id AND length(btrim(f.text))>0) AS text_available
              FROM kb.source s WHERE s.library_id=%s AND s.publication<>'withdrawn'
              ORDER BY s.created_at DESC,s.id LIMIT 101""",
            (library_id,),
        )
        rows = cur.fetchall()
        return {
            "sources": rows[:100],
            "truncated": len(rows) > 100,
            "can_generate": access["admin"] is True
            and access["rank"] >= 20
            and conn.execute(
                "SELECT kb.library_policy_allows(%s,'generation')", (library_id,)
            ).fetchone()[0],
        }


def submit_research(
    conn: Any,
    principal: Principal,
    library_id: UUID,
    source_ids: list[UUID],
    kind: str,
    query: str,
    idempotency_key: str,
) -> dict:
    if kind not in KINDS or len(query) > 2000 or not 8 <= len(idempotency_key) <= 200:
        raise ValueError("invalid_research_request")
    if kind == "research" and not query.strip():
        raise ValueError("question_required")
    if not 1 <= len(source_ids) <= 10 or len(set(source_ids)) != len(source_ids):
        raise ValueError("select_distinct_sources")
    if kind == "comparison" and len(source_ids) < 2:
        raise ValueError("comparison_requires_two_sources")
    request_hash = hashlib.sha256(
        json.dumps(
            {
                "library_id": str(library_id),
                "sources": sorted(map(str, source_ids)),
                "kind": kind,
                "query": query,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        _library(cur, principal, library_id, generation=True)
        # Serialize only this actor's submission admission, including the
        # idempotency lookup and bounded backlog. No network runs in this lock.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"research-submit:{principal.principal_id}",),
        )
        cur.execute(
            "SELECT id,account_id,request_hash,status FROM kb.research_run "
            "WHERE principal_id=%s AND idempotency_key=%s",
            (principal.principal_id, idempotency_key),
        )
        previous = cur.fetchone()
        if previous is not None:
            if (
                previous["account_id"] != principal.account_id
                or previous["request_hash"] != request_hash
            ):
                raise ValueError("idempotency_conflict")
            return {
                "run_id": previous["id"],
                "status": previous["status"],
                "duplicate_of_existing": True,
            }
        # The scope is frozen before queueing. All dependencies are rechecked
        # by the worker; no browser body or caller identity enters the queue.
        snapshot = capture_snapshot(conn, principal, source_ids, query)
        manifest = {**snapshot["manifest"], "context_limited": snapshot["context_limited"]}
        cur.execute(
            "SELECT kb.research_generation_allowed(%s,%s,%s) AS allowed",
            (principal.account_id, library_id, Jsonb(manifest)),
        )
        if cur.fetchone()["allowed"] is not True:
            raise AccessDenied("forbidden")
        check_admission(cur, principal)
        ident = uuid4()
        cur.execute(
            """INSERT INTO kb.research_run(id,account_id,principal_id,library_id,source_ids,
              manifest,kind,query,idempotency_key,request_hash)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                ident,
                principal.account_id,
                principal.principal_id,
                library_id,
                source_ids,
                Jsonb(manifest),
                kind,
                query,
                idempotency_key,
                request_hash,
            ),
        )
        from kb.jobs.research import register_research_task

        app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=""))
        task = register_research_task(app, "")
        queue_id = task.configure(connection=conn, lock=PROVIDER_QUEUE_LOCK).defer(
            run_id=str(ident),
            principal_id=str(principal.principal_id),
            account_id=str(principal.account_id),
        )
        cur.execute("UPDATE kb.research_run SET queue_id=%s WHERE id=%s", (queue_id, ident))
        return {"run_id": ident, "status": "queued", "duplicate_of_existing": False}


def checked_run(cur: Any, principal: Principal, run_id: UUID) -> dict:
    cur.execute(
        "SELECT * FROM kb.research_run WHERE id=%s AND principal_id=%s AND account_id=%s",
        (run_id, principal.principal_id, principal.account_id),
    )
    row = cur.fetchone()
    if row is None:
        raise AccessDenied("forbidden")
    check_snapshot(cur, row["manifest"])
    if row["status"] == "done":
        result = dict(row["result"])
        result["citations"] = [
            validate_citation(cur, row["manifest"], cite) for cite in result.get("citations", [])
        ]
        row["result"] = result
    return dict(row)


def get_research(conn: Any, principal: Principal, run_id: UUID) -> dict:
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        row = checked_run(cur, principal, run_id)
        return {
            key: row[key]
            for key in (
                "id",
                "status",
                "kind",
                "query",
                "source_ids",
                "created_at",
                "completed_at",
                "error_code",
                "result",
            )
        }


def list_research(conn, principal, library_id):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        _library(cur, principal, library_id)
        cur.execute(
            "SELECT id,kind,query,status,created_at FROM kb.research_run "
            "WHERE library_id=%s ORDER BY created_at DESC,id LIMIT 51",
            (library_id,),
        )
        rows = cur.fetchall()
        return {"runs": rows[:50], "truncated": len(rows) > 50}
