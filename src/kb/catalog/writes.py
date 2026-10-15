"""Versioned draft publication and explicit, append-only experience records."""

from typing import Any
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog.retrieval_events import record_retrieval


class RuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    when_to_apply: str | None = Field(default=None, max_length=2000)
    expected_effect: str | None = Field(default=None, max_length=2000)
    verification: str | None = Field(default=None, max_length=2000)
    inputs: list[str] = Field(default_factory=list, max_length=20)
    preconditions: list[str] = Field(default_factory=list, max_length=20)
    actions: list[str] = Field(default_factory=list, max_length=20)
    exceptions: list[str] = Field(default_factory=list, max_length=20)
    fragment_ids: list[UUID] = Field(default_factory=list, max_length=20)


def create_rule(conn, principal, library_id, body: RuleInput, replaces: UUID | None = None):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        lineage, version = None, 1
        if replaces:
            cur.execute(
                "SELECT lineage_id,library_id FROM kb.rule WHERE id=%s FOR UPDATE", (replaces,)
            )
            previous = cur.fetchone()
            if not previous or previous["library_id"] != library_id:
                raise AccessDenied("forbidden")
            lineage = previous["lineage_id"]
            # Lock the lineage root so two corrections cannot allocate the same version.
            cur.execute("SELECT id FROM kb.rule WHERE id=%s FOR UPDATE", (lineage,))
            cur.execute(
                "SELECT max(version_no)+1 AS n FROM kb.rule WHERE lineage_id=%s", (lineage,)
            )
            version = cur.fetchone()["n"]
        ident = uuid4()
        cur.execute(
            """INSERT INTO kb.rule(id,library_id,lineage_id,version_no,title,when_to_apply,
          expected_effect,verification,inputs,preconditions,exceptions)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                ident,
                library_id,
                lineage or ident,
                version,
                body.title,
                body.when_to_apply,
                body.expected_effect,
                body.verification,
                Jsonb(body.inputs),
                Jsonb(body.preconditions),
                Jsonb(body.exceptions),
            ),
        )
        result = dict(cur.fetchone())
        for ordinal, action in enumerate(body.actions):
            cur.execute(
                "INSERT INTO kb.rule_action(rule_id,ordinal,body) VALUES(%s,%s,%s)",
                (ident, ordinal, action),
            )
        for fragment in body.fragment_ids:
            cur.execute("SELECT source_id FROM kb.fragment WHERE id=%s", (fragment,))
            row = cur.fetchone()
            if row is None:
                raise AccessDenied("forbidden")
            cur.execute(
                """INSERT INTO kb.rule_provenance(rule_id,source_id,fragment_id)
              VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                (ident, row["source_id"], fragment),
            )
        return result


def publish(conn, principal, ident, *, knowledge=False):
    with transaction_identity(conn, principal):
        # Identifiers are fixed literals; never interpolate request values.
        query = (
            "UPDATE kb.knowledge SET publication='published' WHERE id=%s RETURNING id"
            if knowledge
            else "UPDATE kb.rule SET publication='published' WHERE id=%s RETURNING id"
        )
        row = conn.execute(query, (ident,)).fetchone()
        if not row:
            raise AccessDenied("forbidden")
        return {"id": str(row[0]), "publication": "published"}


def reject(conn, principal, ident, *, knowledge=False):
    """Withdraw a draft or a rule awaiting review, for a curator of *that* library.

    Rejection is a change of publication_status and nothing else: the row, its
    provenance and its version history are left exactly as they were. There is
    no ``reason`` parameter, because there is nowhere to put one.

    **The authority test is part of the mutation, not a statement before it.**
    An earlier version read the role in its own SELECT and then issued a bare
    ``UPDATE ... WHERE id=%s``. Nothing locked ``kb.library_grant`` between the
    two statements, so a demotion committed in that window was invisible to the
    read and visible to the write; the row lock was on the rule, not on the
    grant. RLS did not save it either: ``knowledge_write``/``rule_write`` only
    require contributor, and the 0006 triggers only guard a row becoming
    *published*, not one becoming withdrawn. One statement means one snapshot,
    so the authority to write and the decision to write are the same decision.

    The mutation therefore carries its own predicates, and a refusal is a zero
    row count reported as ``AccessDenied`` — the same answer as a row that was
    never there, and as a row the caller cannot see. Repeats are answered by a
    second statement that re-runs the identical predicates against the state
    *now*, so a curator whose grant or membership has gone answers 403 even
    though the row is already withdrawn and the answer would otherwise be the
    same.
    """
    if principal.generation_watermark < 1:
        raise AccessDenied("forbidden")
    with transaction_identity(conn, principal), conn.cursor() as cur:
        # Identifiers are fixed literals; never interpolate request values.
        # 30 is kb.role_rank('curator'): the same bar the 0006 triggers set for
        # publication, and the same one the RLS policies use for curation.
        update = (
            """UPDATE kb.knowledge SET publication='withdrawn'
          WHERE id=%s AND publication IN ('draft','needs_review')
            AND COALESCE(kb.role_rank(kb.effective_role(%s, kb.knowledge.library_id)),0) >= 30
            AND kb.membership_allows(%s)
            AND EXISTS (SELECT 1 FROM kb.library l JOIN kb.membership m
                          ON m.organisation_id = l.organisation_id
                        WHERE l.id = kb.knowledge.library_id
                          AND m.principal_id = %s AND m.status = 'active')
          RETURNING id"""
            if knowledge
            else """UPDATE kb.rule SET publication='withdrawn'
          WHERE id=%s AND publication IN ('draft','needs_review')
            AND COALESCE(kb.role_rank(kb.effective_role(%s, kb.rule.library_id)),0) >= 30
            AND kb.membership_allows(%s)
            AND EXISTS (SELECT 1 FROM kb.library l JOIN kb.membership m
                          ON m.organisation_id = l.organisation_id
                        WHERE l.id = kb.rule.library_id
                          AND m.principal_id = %s AND m.status = 'active')
          RETURNING id"""
        )
        cur.execute(
            update, (ident, principal.principal_id, principal.principal_id, principal.principal_id)
        )
        if cur.rowcount:
            return {"id": str(ident), "publication": "withdrawn"}

        # Nothing was written. The identical predicates run again, now, to tell
        # an already-withdrawn row from a published one from a refusal. They
        # are not a cache of the first statement: this is a second read of the
        # grant and the membership, under this statement's snapshot.
        classify = (
            """SELECT id,publication FROM kb.knowledge WHERE id=%s
            AND COALESCE(kb.role_rank(kb.effective_role(%s, kb.knowledge.library_id)),0) >= 30
            AND kb.membership_allows(%s)
            AND EXISTS (SELECT 1 FROM kb.library l JOIN kb.membership m
                          ON m.organisation_id = l.organisation_id
                        WHERE l.id = kb.knowledge.library_id
                          AND m.principal_id = %s AND m.status = 'active')"""
            if knowledge
            else """SELECT id,publication FROM kb.rule WHERE id=%s
            AND COALESCE(kb.role_rank(kb.effective_role(%s, kb.rule.library_id)),0) >= 30
            AND kb.membership_allows(%s)
            AND EXISTS (SELECT 1 FROM kb.library l JOIN kb.membership m
                          ON m.organisation_id = l.organisation_id
                        WHERE l.id = kb.rule.library_id
                          AND m.principal_id = %s AND m.status = 'active')"""
        )
        cur.execute(
            classify,
            (ident, principal.principal_id, principal.principal_id, principal.principal_id),
        )
        row = cur.fetchone()
        if row is None:
            # Not here, not visible, not a curator, not an active member: one
            # answer, because the difference is a directory of invisible rows.
            raise AccessDenied("forbidden")
        if row[1] == "withdrawn":
            return {"id": str(row[0]), "publication": "withdrawn"}
        if row[1] == "published":
            raise ValueError("a published item is not withdrawn here")
        # draft or needs_review, and yet the mutation matched nothing. Refused
        # rather than reported as a withdrawal: a zero count is not a success.
        raise AccessDenied("forbidden")


def start_use(
    conn,
    principal,
    rule_id,
    version_no,
    expected_result=None,
    where_applied=None,
    *,
    idempotency_key=None,
):
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 200
    ):
        raise ValueError("invalid_idempotency_key")
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        if idempotency_key is not None:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"experience:start_use:{principal.principal_id}:{idempotency_key}",),
            )
        cur.execute(
            "SELECT id FROM kb.rule WHERE id=%s AND version_no=%s AND publication='published'",
            (rule_id, version_no),
        )
        if cur.fetchone() is None:
            raise AccessDenied("forbidden")
        if idempotency_key is not None:
            cur.execute(
                """SELECT u.*,e.expected_result,e.where_applied FROM kb.use_record u
                  JOIN kb.experience e ON e.use_id=u.id
                  WHERE u.principal_id=%s AND u.idempotency_key=%s""",
                (principal.principal_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                use = dict(existing)
                expected = use.pop("expected_result")
                location = use.pop("where_applied")
                if (use["rule_id"], use["rule_version_no"], expected, location) != (
                    rule_id,
                    version_no,
                    expected_result,
                    where_applied,
                ):
                    raise ValueError("idempotency_conflict")
                return use
        cur.execute(
            """INSERT INTO kb.use_record(rule_id,rule_version_no,principal_id,idempotency_key)
              VALUES(%s,%s,%s,%s) RETURNING *""",
            (rule_id, version_no, principal.principal_id, idempotency_key),
        )
        use = dict(cur.fetchone())
        cur.execute(
            """INSERT INTO kb.experience(use_id,rule_id,rule_version_no,expected_result,
              where_applied) VALUES(%s,%s,%s,%s,%s)""",
            (use["id"], rule_id, version_no, expected_result, where_applied),
        )
        return use


def mark_applied(conn, principal, use_id):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT u.id,u.state FROM kb.use_record u JOIN kb.rule r
              ON r.id=u.rule_id AND r.version_no=u.rule_version_no
              WHERE u.id=%s AND u.principal_id=%s AND r.publication='published'
              FOR UPDATE OF u""",
            (use_id, principal.principal_id),
        )
        use = cur.fetchone()
        if use is None:
            raise AccessDenied("forbidden")
        if use["state"] == "retrieved":
            cur.execute(
                "UPDATE kb.use_record SET state='applied' WHERE id=%s RETURNING id,state",
                (use_id,),
            )
            return dict(cur.fetchone())
        if use["state"] not in ("applied", "outcomed", "corrected"):
            raise AccessDenied("forbidden")
        return dict(use)


def record_outcome(
    conn,
    principal,
    use_id,
    observed_result=None,
    rating=None,
    rating_basis=None,
    is_correction=False,
    *,
    idempotency_key=None,
    achievement=None,
    assessor_kind="unknown",
    assessment_evidence=None,
):
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str)
        or not idempotency_key.strip()
        or len(idempotency_key) > 200
    ):
        raise ValueError("invalid_idempotency_key")
    if achievement is not None and (
        not isinstance(achievement, str)
        or achievement not in {"met", "partly", "not_met", "unknown"}
    ):
        raise ValueError("invalid_achievement")
    if not isinstance(assessor_kind, str) or assessor_kind not in {
        "human",
        "agent",
        "measurement",
        "unknown",
    }:
        raise ValueError("invalid_assessor_kind")
    if assessment_evidence is not None and (
        not isinstance(assessment_evidence, str) or len(assessment_evidence) > 4000
    ):
        raise ValueError("invalid_assessment_evidence")
    if assessor_kind == "measurement" and (
        assessment_evidence is None or not assessment_evidence.strip()
    ):
        raise ValueError("measurement_evidence_required")
    if not any(value is not None for value in (observed_result, rating, rating_basis, achievement)):
        raise ValueError("an outcome must contain a reported value")
    persisted_achievement = "unknown" if achievement is None else achievement
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        if idempotency_key is not None:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (f"experience:outcome:{principal.principal_id}:{idempotency_key}",),
            )
        cur.execute(
            """SELECT u.state FROM kb.use_record u JOIN kb.rule r
              ON r.id=u.rule_id AND r.version_no=u.rule_version_no
              WHERE u.id=%s AND u.principal_id=%s AND r.publication='published'
              FOR UPDATE OF u""",
            (use_id, principal.principal_id),
        )
        use = cur.fetchone()
        if use is None:
            raise AccessDenied("forbidden")
        if idempotency_key is not None:
            cur.execute(
                "SELECT * FROM kb.outcome WHERE reported_by=%s AND idempotency_key=%s",
                (principal.principal_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                if (
                    existing["use_id"],
                    existing["observed_result"],
                    existing["rating"],
                    existing["rating_basis"],
                    existing["achievement"],
                    existing["assessor_kind"],
                    existing["assessment_evidence"],
                    existing["is_correction"],
                ) != (
                    use_id,
                    observed_result,
                    rating,
                    rating_basis,
                    persisted_achievement,
                    assessor_kind,
                    assessment_evidence,
                    is_correction,
                ):
                    raise ValueError("idempotency_conflict")
                return dict(existing)
        if use["state"] == "retrieved":
            raise ValueError("confirm application before reporting an outcome")
        cur.execute("SELECT count(*) AS n FROM kb.outcome WHERE use_id=%s", (use_id,))
        exists = cur.fetchone()["n"] > 0
        if exists != is_correction:
            raise ValueError("a later outcome requires explicit correction")
        cur.execute(
            """INSERT INTO kb.outcome(
              use_id,observed_result,rating,rating_basis,
              achievement,assessor_kind,assessment_evidence,
              reported_by,reported_at,is_correction,idempotency_key
            )
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s)
            RETURNING *""",
            (
                use_id,
                observed_result,
                rating,
                rating_basis,
                persisted_achievement,
                assessor_kind,
                assessment_evidence,
                principal.principal_id,
                is_correction,
                idempotency_key,
            ),
        )
        result = dict(cur.fetchone())
        cur.execute(
            "UPDATE kb.use_record SET state=%s WHERE id=%s",
            ("corrected" if exists else "outcomed", use_id),
        )
        return result


def list_uses(conn, principal):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""SELECT u.*,e.expected_result,e.where_applied,r.title FROM kb.use_record u
          JOIN kb.experience e ON e.use_id=u.id JOIN kb.rule r ON r.id=u.rule_id
          ORDER BY u.started_at DESC LIMIT 100""")
        records = [dict(r) for r in cur.fetchall()]
        for record in records:
            cur.execute(
                "SELECT * FROM kb.outcome WHERE use_id=%s ORDER BY reported_at,id", (record["id"],)
            )
            record["outcomes"] = [dict(r) for r in cur.fetchall()]
        return records


def job_status(conn, principal, job_id):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """SELECT id,source_id,status,error_code,chunks_done,chunks_total,
          extraction_enabled,created_at,completed_at FROM kb.job WHERE id=%s""",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        return dict(row)


def list_knowledge(conn, principal, library_id):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM kb.knowledge WHERE library_id=%s ORDER BY created_at LIMIT 500",
            (library_id,),
        )
        records = [dict(r) for r in cur.fetchall()]
        for record in records:
            record_retrieval(cur, principal, item_kind="knowledge", item_id=record["id"])
        return records


def update_metadata(conn, principal, source_id, values: dict[str, Any]):
    if set(values) - {"author", "edition", "publication_ref"}:
        raise ValueError("unsupported metadata")
    with transaction_identity(conn, principal):
        if not values:
            raise ValueError("no metadata fields supplied")
        assignments = sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in values)
        row = conn.execute(
            sql.SQL("UPDATE kb.source SET {} WHERE id=%s RETURNING id").format(assignments),
            [*values.values(), source_id],
        ).fetchone()
        if not row:
            raise AccessDenied("forbidden")
        return {"id": str(row[0])}
