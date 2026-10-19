"""Persisted typed semantic drafts; evidence and endpoints are checked on every read."""

from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog.idempotency import (
    SEMANTIC_CONCEPT_CREATE,
    SEMANTIC_RELATION_CREATE,
    find_create_operation,
    hash_create_request,
    lock_create_operation,
    record_create_operation,
    validate_idempotency_key,
)
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


def _assert_semantic_create_access(cur, principal, library_id):
    access = _library(cur, principal, library_id)
    if access["rank"] < 20:
        raise AccessDenied("forbidden")


def _validate_semantic_replay_sources(cur, row):
    """Revalidate inherited evidence instead of trusting an old create response."""
    if row["manifest"] is not None:
        check_snapshot(cur, row["manifest"])
        for citation in row["citations"] or []:
            validate_citation(cur, row["manifest"], citation)


def create_concept(conn, principal, library_id, label, body, replaces=None, idempotency_key=None):
    if not label.strip() or len(label) > 500 or not body.strip() or len(body) > 2000:
        raise ValueError("invalid_concept")
    key = validate_idempotency_key(idempotency_key)
    request_digest = (
        hash_create_request(
            SEMANTIC_CONCEPT_CREATE,
            {
                "library_id": str(library_id),
                "label": label,
                "body": body,
                "replaces": str(replaces) if replaces is not None else None,
            },
        )
        if key is not None
        else None
    )
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        if key is not None:
            assert request_digest is not None
            lock_create_operation(cur, principal, SEMANTIC_CONCEPT_CREATE, key)
            receipt = find_create_operation(cur, principal, SEMANTIC_CONCEPT_CREATE, key)
            if receipt is not None:
                _assert_semantic_create_access(cur, principal, library_id)
                if receipt.request_hash != request_digest:
                    raise ValueError("idempotency_conflict")
                cur.execute(
                    """SELECT id,publication,version_no,manifest,citations
                       FROM kb.semantic_concept
                       WHERE id=%s AND library_id=%s LIMIT 1""",
                    (receipt.target_id, library_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise AccessDenied("forbidden")
                _validate_semantic_replay_sources(cur, row)
                return {
                    "id": row["id"],
                    "publication": row["publication"],
                    "version_no": row["version_no"],
                }

        _assert_semantic_create_access(cur, principal, library_id)
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
                _validate_semantic_replay_sources(cur, old)
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
        created = dict(cur.fetchone())
        if key is not None:
            assert request_digest is not None
            record_create_operation(
                cur,
                principal,
                SEMANTIC_CONCEPT_CREATE,
                key,
                request_digest,
                created["id"],
            )
        return created


def _lock_semantic_endpoint(cur, concept_id):
    """Use an RLS-neutral transaction lock shared with concept withdrawal."""
    cur.execute(
        "SELECT pg_advisory_xact_lock("
        "hashtextextended(('semantic-endpoint:' || %s::uuid)::text,0))",
        (concept_id,),
    )


def _assert_semantic_concept_review_access(cur, principal, concept_id):
    """Require SELECT visibility and review capability before endpoint locking."""
    cur.execute(
        "SELECT library_id FROM kb.semantic_concept WHERE id=%s LIMIT 1",
        (concept_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise AccessDenied("forbidden")
    access = _library(cur, principal, row["library_id"])
    if access["rank"] < 30:
        raise AccessDenied("forbidden")


def _assert_relation_endpoints_visible(cur, library_id, from_concept, to_concept):
    """Validate endpoints under SELECT RLS and order races with withdrawal."""
    endpoint_sql = (
        "SELECT id FROM kb.semantic_concept WHERE id=ANY(%s::uuid[]) "
        "AND library_id=%s AND publication<>'withdrawn'"
    )
    endpoint_params = ([from_concept, to_concept], library_id)
    cur.execute(endpoint_sql, endpoint_params)
    visible_ids = [row["id"] for row in cur.fetchall()]
    if len(visible_ids) != 2:
        raise AccessDenied("forbidden")

    # Do not let invisible IDs take locks; canonical order prevents deadlocks.
    for endpoint_id in sorted(visible_ids, key=str):
        _lock_semantic_endpoint(cur, endpoint_id)

    # A withdrawal may have acquired its lock between the two visibility reads.
    cur.execute(endpoint_sql, endpoint_params)
    if len(cur.fetchall()) != 2:
        raise AccessDenied("forbidden")
    # For a new relation, relation INSERT RLS performs the final atomic check.


def create_relation(
    conn,
    principal,
    library_id,
    from_concept,
    to_concept,
    predicate,
    hypothesis=True,
    replaces=None,
    idempotency_key=None,
):
    if from_concept == to_concept or predicate not in {
        "supports",
        "part_of",
        "used_for",
        "contradicts",
    }:
        raise ValueError("invalid_relation")
    key = validate_idempotency_key(idempotency_key)
    request_digest = (
        hash_create_request(
            SEMANTIC_RELATION_CREATE,
            {
                "library_id": str(library_id),
                "from_concept": str(from_concept),
                "to_concept": str(to_concept),
                "predicate": predicate,
                "hypothesis": hypothesis,
                "replaces": str(replaces) if replaces is not None else None,
            },
        )
        if key is not None
        else None
    )
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        if key is not None:
            assert request_digest is not None
            lock_create_operation(cur, principal, SEMANTIC_RELATION_CREATE, key)
            receipt = find_create_operation(cur, principal, SEMANTIC_RELATION_CREATE, key)
            if receipt is not None:
                _assert_semantic_create_access(cur, principal, library_id)
                if receipt.request_hash != request_digest:
                    raise ValueError("idempotency_conflict")
                _assert_relation_endpoints_visible(cur, library_id, from_concept, to_concept)
                cur.execute(
                    """SELECT id,publication,version_no,manifest,citations
                       FROM kb.semantic_relation
                       WHERE id=%s AND library_id=%s LIMIT 1""",
                    (receipt.target_id, library_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise AccessDenied("forbidden")
                _validate_semantic_replay_sources(cur, row)
                return {
                    "id": row["id"],
                    "publication": row["publication"],
                    "version_no": row["version_no"],
                }

        _assert_semantic_create_access(cur, principal, library_id)
        _assert_relation_endpoints_visible(cur, library_id, from_concept, to_concept)
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
                _validate_semantic_replay_sources(cur, old)
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
        created = dict(cur.fetchone())
        if key is not None:
            assert request_digest is not None
            record_create_operation(
                cur,
                principal,
                SEMANTIC_RELATION_CREATE,
                key,
                request_digest,
                created["id"],
            )
        return created


def review_semantic(conn, principal, entity_id, entity_kind, decision):
    if entity_kind not in {"concept", "relation"} or decision not in {"publish", "withdraw"}:
        raise ValueError("invalid_review")
    from psycopg import sql

    table = sql.Identifier("kb", "semantic_" + entity_kind)
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        # Relation creation locks endpoints before FK checks; preserve that order.
        if entity_kind == "concept" and decision == "withdraw":
            # Establish SELECT RLS visibility and current curator/manager authority
            # before an invisible or unauthorized ID can contend on this lock.
            _assert_semantic_concept_review_access(cur, principal, entity_id)
            _lock_semantic_endpoint(cur, entity_id)
            # A grant or SELECT policy may have changed while this backend waited.
            _assert_semantic_concept_review_access(cur, principal, entity_id)
        # SELECT FOR UPDATE remains the post-lock state recheck and row lock.
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
