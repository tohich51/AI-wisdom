"""Persisted typed semantic drafts; evidence and endpoints are checked on every read."""

from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.access.policy import AccessDenied, transaction_identity
from kb.research.grounding import (
    check_snapshot,
    validate_citation,
    validate_citations,
    validate_graph_citations,
)
from kb.research.runs import _library


def import_network(cur, principal, library_id, run_id, manifest, result):
    check_snapshot(cur, manifest)
    concepts = []
    citations = {c["id"]: c for c in result["citations"]}
    for index, item in enumerate(result["items"]):
        ident = uuid4()
        cur.execute(
            """INSERT INTO kb.semantic_concept(id,library_id,principal_id,lineage_id,version_no,
              label,body,origin,origin_run,origin_item,manifest,citations)
              VALUES(%s,%s,%s,%s,1,%s,%s,'generated',%s,%s,%s,%s)
              ON CONFLICT(origin_run,origin_item) DO NOTHING RETURNING id""",
            (
                ident,
                library_id,
                principal.principal_id,
                ident,
                item["heading"] or item["text"][:500],
                item["text"],
                run_id,
                index,
                Jsonb(manifest),
                Jsonb(jsonable_encoder([citations[x] for x in item["citation_ids"]])),
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT id FROM kb.semantic_concept WHERE origin_run=%s AND origin_item=%s",
                (run_id, index),
            )
            row = cur.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        concepts.append(row["id"])
    for index, rel in enumerate(result["relations"]):
        ident = uuid4()
        cur.execute(
            """INSERT INTO kb.semantic_relation(id,library_id,principal_id,lineage_id,version_no,
              from_concept,to_concept,predicate,hypothesis,origin,origin_run,origin_item,manifest,citations)
              VALUES(%s,%s,%s,%s,1,%s,%s,%s,%s,'generated',%s,%s,%s,%s)
              ON CONFLICT(origin_run,origin_item) DO NOTHING""",
            (
                ident,
                library_id,
                principal.principal_id,
                ident,
                concepts[rel["from_item"]],
                concepts[rel["to_item"]],
                rel["predicate"],
                rel["hypothesis"],
                run_id,
                index,
                Jsonb(manifest),
                Jsonb(jsonable_encoder([citations[x] for x in rel["citation_ids"]])),
            ),
        )


def _concept_node(cur, row, evidence=None):
    if evidence is None:
        evidence = []
        if row["manifest"] is not None:
            evidence = validate_citations(cur, row["manifest"], row["citations"])
    return {
        "id": "concept:" + str(row["id"]),
        "entity_id": row["id"],
        "kind": "concept",
        "label": row["label"],
        "body": row["body"],
        "library_id": row["library_id"],
        "publication": row["publication"],
        "origin": row["origin"],
        "version_no": row["version_no"],
        "evidence": evidence,
        "basis": (
            "manual_revision_with_inherited_sources"
            if row["origin"] == "manual_revision"
            else "source_grounded_draft"
            if evidence
            else "manual_without_evidence"
        ),
    }


def get_concept(conn, principal, library_id, concept_id):
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        _library(cur, principal, library_id)
        cur.execute(
            "SELECT * FROM kb.semantic_concept WHERE id=%s AND library_id=%s "
            "AND publication<>'withdrawn' LIMIT 1",
            (concept_id, library_id),
        )
        row = cur.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        return _concept_node(cur, row)


def semantic_network(conn, principal, library_id, limit=200):
    if not 1 <= limit <= 500:
        raise ValueError("invalid_graph_limit")
    from kb.research.similarity import concept_text_hash, similarity_layer

    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        access = _library(cur, principal, library_id)
        cur.execute(
            "SELECT * FROM kb.semantic_concept WHERE library_id=%s AND publication<>'withdrawn' "
            "ORDER BY created_at,id LIMIT %s",
            (library_id, limit + 1),
        )
        rows = cur.fetchall()
        selected_rows = rows[:limit]
        ids = [row["id"] for row in selected_rows]
        node_hashes = {
            row["id"]: concept_text_hash(row["label"], row["body"]) for row in selected_rows
        }
        cur.execute(
            """SELECT * FROM kb.semantic_relation WHERE library_id=%s
                     AND publication<>'withdrawn' AND
                     from_concept=ANY(%s::uuid[]) AND to_concept=ANY(%s::uuid[])
                     ORDER BY created_at,id LIMIT %s""",
            (library_id, ids, ids, limit * 3 + 1),
        )
        rels = cur.fetchall()
        selected_rels = rels[: limit * 3]
        similarity = similarity_layer(cur, principal, library_id, node_hashes, limit)

        records = [(row["manifest"], row["citations"]) for row in selected_rows]
        records.extend((row["manifest"], row["citations"]) for row in selected_rels)
        prevalidated = validate_graph_citations(cur, records)
        nodes = [
            _concept_node(cur, row, evidence)
            for row, evidence in zip(selected_rows, prevalidated[: len(selected_rows)], strict=True)
        ]
        edges = []
        for row, evidence in zip(selected_rels, prevalidated[len(selected_rows) :], strict=True):
            edges.append(
                {
                    "id": "relation:" + str(row["id"]),
                    "entity_id": row["id"],
                    "source": "concept:" + str(row["from_concept"]),
                    "target": "concept:" + str(row["to_concept"]),
                    "predicate": row["predicate"],
                    "hypothesis": row["hypothesis"],
                    "publication": row["publication"],
                    "origin": row["origin"],
                    "version_no": row["version_no"],
                    "evidence": evidence,
                    "basis": (
                        "manual_revision_with_inherited_sources"
                        if row["origin"] == "manual_revision"
                        else "inferred_from_sources"
                        if evidence
                        else "manual_without_evidence"
                    ),
                }
            )
        return {
            "nodes": nodes,
            "edges": edges,
            "mode": "semantic",
            "truncated": len(rows) > limit or len(rels) > limit * 3,
            "capabilities": {
                "can_create": access["rank"] >= 20,
                "can_review": access["rank"] >= 30,
            },
            "similarity": similarity,
        }


def create_concept(conn, principal, library_id, label, body, replaces=None):
    if not label.strip() or len(label) > 500 or not body.strip() or len(body) > 2000:
        raise ValueError("invalid_concept")
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        _library(cur, principal, library_id)
        ident = uuid4()
        lineage = ident
        version = 1
        origin, manifest, citations = "manual", None, []
        if replaces:
            cur.execute("SELECT * FROM kb.semantic_concept WHERE id=%s FOR UPDATE", (replaces,))
            old = cur.fetchone()
            if old is None or old["library_id"] != library_id:
                raise AccessDenied("forbidden")
            lineage = old["lineage_id"]
            if old["manifest"] is not None:
                check_snapshot(cur, old["manifest"])
                origin, manifest, citations = "manual_revision", old["manifest"], old["citations"]
            cur.execute("SELECT id FROM kb.semantic_concept WHERE id=%s FOR UPDATE", (lineage,))
            cur.execute(
                "SELECT max(version_no)+1 AS n FROM kb.semantic_concept WHERE lineage_id=%s",
                (lineage,),
            )
            version = cur.fetchone()["n"]
        cur.execute(
            """INSERT INTO kb.semantic_concept(id,library_id,principal_id,lineage_id,version_no,
                     label,body,origin,manifest,citations) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) 
            RETURNING id,publication,version_no""",
            (
                ident,
                library_id,
                principal.principal_id,
                lineage,
                version,
                label,
                body,
                origin,
                Jsonb(manifest) if manifest is not None else None,
                Jsonb(jsonable_encoder(citations)),
            ),
        )
        return dict(cur.fetchone())


def create_relation(
    conn, principal, library_id, from_concept, to_concept, predicate, hypothesis=True, replaces=None
):
    if from_concept == to_concept or predicate not in {
        "supports",
        "part_of",
        "used_for",
        "contradicts",
    }:
        raise ValueError("invalid_relation")
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        _library(cur, principal, library_id)
        # Both endpoints must be visible in the same library before any insert.
        cur.execute(
            "SELECT id FROM kb.semantic_concept WHERE id=ANY(%s::uuid[]) AND library_id=%s",
            ([from_concept, to_concept], library_id),
        )
        if len(cur.fetchall()) != 2:
            raise AccessDenied("forbidden")
        ident = uuid4()
        lineage = ident
        version = 1
        origin, manifest, citations = "manual", None, []
        if replaces:
            cur.execute("SELECT * FROM kb.semantic_relation WHERE id=%s FOR UPDATE", (replaces,))
            old = cur.fetchone()
            if old is None or old["library_id"] != library_id:
                raise AccessDenied("forbidden")
            lineage = old["lineage_id"]
            if old["manifest"] is not None:
                check_snapshot(cur, old["manifest"])
                origin, manifest, citations = "manual_revision", old["manifest"], old["citations"]
            cur.execute("SELECT id FROM kb.semantic_relation WHERE id=%s FOR UPDATE", (lineage,))
            cur.execute(
                "SELECT max(version_no)+1 AS n FROM kb.semantic_relation WHERE lineage_id=%s",
                (lineage,),
            )
            version = cur.fetchone()["n"]
        cur.execute(
            """INSERT INTO kb.semantic_relation(id,library_id,principal_id,lineage_id,version_no,
              from_concept,to_concept,predicate,hypothesis,origin,manifest,citations)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,publication,version_no""",
            (
                ident,
                library_id,
                principal.principal_id,
                lineage,
                version,
                from_concept,
                to_concept,
                predicate,
                hypothesis,
                origin,
                Jsonb(manifest) if manifest is not None else None,
                Jsonb(jsonable_encoder(citations)),
            ),
        )
        return dict(cur.fetchone())


def review_semantic(conn, principal, entity_id, entity_kind, decision):
    if entity_kind not in {"concept", "relation"} or decision not in {"publish", "withdraw"}:
        raise ValueError("invalid_review")
    from psycopg import sql

    table = sql.Identifier("kb", "semantic_" + entity_kind)
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql.SQL("SELECT * FROM {} WHERE id=%s FOR UPDATE").format(table), (entity_id,))
        row = cur.fetchone()
        if row is None:
            raise AccessDenied("forbidden")
        if row["manifest"] is not None:
            check_snapshot(cur, row["manifest"])
            for citation in row["citations"]:
                validate_citation(cur, row["manifest"], citation)
        if decision == "publish" and entity_kind == "relation":
            cur.execute(
                "SELECT id FROM kb.semantic_concept WHERE id=ANY(%s::uuid[]) "
                "AND publication='published'",
                ([row["from_concept"], row["to_concept"]],),
            )
            if len(cur.fetchall()) != 2:
                raise ValueError("publish_endpoints_first")
        cur.execute(
            sql.SQL("UPDATE {} SET publication=%s WHERE id=%s RETURNING id,publication").format(
                table
            ),
            ("published" if decision == "publish" else "withdrawn", entity_id),
        )
        updated = cur.fetchone()
        if updated is None:
            raise AccessDenied("forbidden")
        return dict(updated)
