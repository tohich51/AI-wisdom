"""Bounded workspace views and curator review over the canonical catalog."""

from typing import Literal

from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import writes


class EditKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=10000)
    expected_statement: str = Field(min_length=1, max_length=10000)
    kind: Literal["assertion", "definition", "recommendation", "example"]


class ReviewBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_kind: Literal["knowledge", "rule"]
    item_ids: list[str] = Field(min_length=1, max_length=100)
    decision: Literal["publish", "reject"]


def edit_knowledge(conn, principal, knowledge_id, body: EditKnowledge):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM kb.knowledge WHERE id=%s AND publication IN ('draft','needs_review') "
            "AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20 FOR UPDATE",
            (knowledge_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        if row["statement"] != body.expected_statement:
            raise ValueError("knowledge_revision_changed")
        if not body.statement.strip():
            raise ValueError("statement_required")
        cur.execute(
            "UPDATE kb.knowledge SET statement=%s,kind=%s WHERE id=%s RETURNING *",
            (body.statement, body.kind, knowledge_id),
        )
        updated = cur.fetchone()
        if updated is None:
            raise AccessDenied("forbidden")
        return dict(updated)


def review_batch(conn, principal, library_id, body: ReviewBatch):
    from uuid import UUID

    identities = sorted({UUID(value) for value in body.item_ids})
    # Atomic: mixed scope/incomplete items roll back every earlier decision.
    with transaction_identity(conn, principal):
        row = conn.execute(
            "SELECT id FROM kb.library WHERE id=%s AND "
            "kb.role_rank(kb.effective_role(kb.current_principal(),id))>=30",
            (library_id,),
        ).fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        select = (
            "SELECT library_id,publication FROM kb.knowledge WHERE id=%s FOR UPDATE"
            if body.item_kind == "knowledge"
            else "SELECT library_id,publication FROM kb.rule WHERE id=%s FOR UPDATE"
        )
        for identity in identities:
            item = conn.execute(select, (identity,)).fetchone()
            if item is None or item[0] != library_id:
                raise AccessDenied("forbidden")
            desired = "published" if body.decision == "publish" else "withdrawn"
            if item[1] == desired:
                continue
            action = writes.publish if body.decision == "publish" else writes.reject
            action(conn, principal, identity, knowledge=body.item_kind == "knowledge")
    return {"reviewed": len(identities), "publication": desired}


def list_jobs(conn, principal, library_id=None):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT j.id,j.library_id,s.title,j.status,j.error_code,j.chunks_done,"
            "j.chunks_total,j.extraction_enabled,j.created_at,j.completed_at "
            "FROM kb.job j LEFT JOIN kb.source s ON s.id=j.source_id "
            "WHERE (%s::uuid IS NULL OR j.library_id=%s) ORDER BY j.created_at DESC,j.id LIMIT 100",
            (library_id, library_id),
        )
        return [dict(row) for row in cur.fetchall()]


def experience_summary(conn, principal):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT u.rule_id,u.rule_version_no,r.title,count(*) AS retrieved,"
            "count(*) FILTER(WHERE u.state<>'retrieved') AS applied,"
            "count(o.id) AS reported,count(*) FILTER(WHERE o.id IS NULL) AS outcome_unknown,"
            "avg(o.rating) AS average_rating,count(o.rating) AS rated "
            "FROM kb.use_record u JOIN kb.rule r ON r.id=u.rule_id "
            "LEFT JOIN LATERAL(SELECT id,rating FROM kb.outcome WHERE use_id=u.id "
            "ORDER BY reported_at DESC,id DESC LIMIT 1) o ON true "
            "GROUP BY u.rule_id,u.rule_version_no,r.title ORDER BY r.title,u.rule_version_no",
        )
        rows = [dict(row) for row in cur.fetchall()]
        return {"basis": "own_reported_experience", "by_rule_version": rows}
