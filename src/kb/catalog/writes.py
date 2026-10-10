"""Versioned draft publication and explicit, append-only experience records."""

from typing import Any
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import AccessDenied, transaction_identity


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


def start_use(conn, principal, rule_id, version_no, expected_result=None, where_applied=None):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """INSERT INTO kb.use_record(rule_id,rule_version_no,principal_id)
          VALUES(%s,%s,%s) RETURNING *""",
            (rule_id, version_no, principal.principal_id),
        )
        use = dict(cur.fetchone())
        cur.execute(
            """INSERT INTO kb.experience(use_id,rule_id,rule_version_no,expected_result,
          where_applied) VALUES(%s,%s,%s,%s,%s)""",
            (use["id"], rule_id, version_no, expected_result, where_applied),
        )
        return use


def mark_applied(conn, principal, use_id):
    with transaction_identity(conn, principal):
        row = conn.execute(
            """UPDATE kb.use_record SET state='applied'
          WHERE id=%s AND state='retrieved' RETURNING id,state""",
            (use_id,),
        ).fetchone()
        if not row:
            raise AccessDenied("forbidden")
        return {"id": str(row[0]), "state": row[1]}


def record_outcome(
    conn,
    principal,
    use_id,
    observed_result=None,
    rating=None,
    rating_basis=None,
    is_correction=False,
):
    if not any(value is not None for value in (observed_result, rating, rating_basis)):
        raise ValueError("an outcome must contain a reported value")
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT state FROM kb.use_record WHERE id=%s FOR UPDATE", (use_id,))
        use = cur.fetchone()
        if not use:
            raise AccessDenied("forbidden")
        if use["state"] == "retrieved":
            raise ValueError("confirm application before reporting an outcome")
        cur.execute("SELECT count(*) AS n FROM kb.outcome WHERE use_id=%s", (use_id,))
        exists = cur.fetchone()["n"] > 0
        if exists != is_correction:
            raise ValueError("a later outcome requires explicit correction")
        cur.execute(
            """INSERT INTO kb.outcome(use_id,observed_result,rating,rating_basis,
          reported_by,reported_at,is_correction) VALUES(%s,%s,%s,%s,%s,now(),%s) RETURNING *""",
            (use_id, observed_result, rating, rating_basis, principal.principal_id, is_correction),
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
        return [dict(r) for r in cur.fetchall()]


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
