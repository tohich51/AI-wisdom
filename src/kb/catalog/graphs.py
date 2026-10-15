"""Bounded provenance graph of actual visible rows; retrieval creates no experience."""

from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog.research import _library


def provenance_graph(conn, principal, library_id, source_ids=None, limit=200):
    if not 1 <= limit <= 500 or (source_ids is not None and not 1 <= len(source_ids) <= 10):
        raise ValueError("invalid_graph_scope")
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        access = _library(cur, principal, library_id)
        if source_ids is not None:
            cur.execute(
                "SELECT id FROM kb.source WHERE id=ANY(%s::uuid[]) AND library_id=%s "
                "AND publication<>'withdrawn'",
                (source_ids, library_id),
            )
            if {r["id"] for r in cur.fetchall()} != set(source_ids):
                raise AccessDenied("forbidden")
        cur.execute(
            """WITH sources AS (SELECT * FROM kb.source WHERE library_id=%s
                       AND publication<>'withdrawn' AND (%s::uuid[] IS NULL OR id=ANY(%s::uuid[]))),
          rules AS (SELECT r.* FROM kb.rule r WHERE r.library_id=%s AND r.publication<>'withdrawn'
                    AND (%s::uuid[] IS NULL OR EXISTS(SELECT 1 FROM kb.rule_provenance p
                       JOIN sources s
                        ON s.id=p.source_id WHERE p.rule_id=r.id))),
          uses AS (SELECT u.* FROM kb.use_record u JOIN rules r ON r.id=u.rule_id
                    AND r.version_no=u.rule_version_no WHERE u.principal_id=%s),
          knowledge AS (
            SELECT k.* FROM kb.knowledge k
             WHERE k.library_id=%s AND k.publication<>'withdrawn' AND (%s::uuid[] IS NULL OR
               EXISTS(SELECT 1 FROM kb.knowledge_provenance p
                 JOIN sources s ON s.id=p.source_id WHERE p.knowledge_id=k.id))
          ),
          proof_fragments AS (
            SELECT DISTINCT p.fragment_id
              FROM kb.knowledge_provenance p
              JOIN knowledge k ON k.id=p.knowledge_id
              JOIN sources s ON s.id=p.source_id
              JOIN kb.fragment f ON f.id=p.fragment_id AND f.source_id=s.id
             WHERE p.fragment_id IS NOT NULL
            UNION
            SELECT DISTINCT p.fragment_id
              FROM kb.rule_provenance p
              JOIN rules r ON r.id=p.rule_id
              JOIN sources s ON s.id=p.source_id
              JOIN kb.fragment f ON f.id=p.fragment_id AND f.source_id=s.id
             WHERE p.fragment_id IS NOT NULL
          ),
          nodes AS (
           SELECT 'source' AS kind,id,title AS label,publication::text,0 AS version_no,1 AS priority
            FROM sources
           UNION ALL SELECT 'fragment',f.id,left(f.text,120),'original',0,
                    CASE WHEN pf.fragment_id IS NULL THEN 7 ELSE 6 END
             FROM kb.fragment f
             JOIN sources s ON s.id=f.source_id
             LEFT JOIN proof_fragments pf ON pf.fragment_id=f.id
           UNION ALL SELECT 'knowledge',k.id,left(k.statement,160),k.publication::text,0,2
             FROM knowledge k
           UNION ALL SELECT 'rule',r.id,r.title,r.publication::text,r.version_no,3 FROM rules r
           UNION ALL SELECT 'use',u.id,u.state::text,'private',u.rule_version_no,4 FROM uses u
           UNION ALL SELECT 'outcome',o.id,COALESCE(left(o.observed_result,160),
             'Оценка: '||o.rating::text),
             'private',0,5
             FROM kb.outcome o JOIN uses u ON u.id=o.use_id
          ) SELECT * FROM nodes ORDER BY priority,id LIMIT %s""",
            (
                library_id,
                source_ids,
                source_ids,
                library_id,
                source_ids,
                principal.principal_id,
                library_id,
                source_ids,
                limit + 1,
            ),
        )
        rows = cur.fetchall()
        nodes = []
        sets = {}
        for row in rows[:limit]:
            node = {
                "id": row["kind"] + ":" + str(row["id"]),
                "entity_id": row["id"],
                "kind": row["kind"],
                "label": row["label"],
                "library_id": library_id,
                "publication": row["publication"],
                "version_no": row["version_no"] or None,
            }
            nodes.append(node)
            sets.setdefault(row["kind"], []).append(row["id"])
        edges = []

        def add(a, aid, b, bid, predicate, basis, evidence=None):
            edges.append(
                {
                    "id": f"{predicate}:{aid}:{bid}",
                    "source": f"{a}:{aid}",
                    "target": f"{b}:{bid}",
                    "predicate": predicate,
                    "basis": basis,
                    "evidence": evidence,
                }
            )

        cur.execute(
            "SELECT id,source_id,locator_kind,file_page,chapter,paragraph,ordinal FROM kb.fragment "
            "WHERE id=ANY(%s::uuid[])",
            (sets.get("fragment", []),),
        )
        for f in cur.fetchall():
            if f["source_id"] in sets.get("source", []):
                add("source", f["source_id"], "fragment", f["id"], "contains", "canonical", dict(f))
        for table, entity in [("knowledge_provenance", "knowledge"), ("rule_provenance", "rule")]:
            from psycopg import sql

            cur.execute(
                sql.SQL(
                    "SELECT source_id,fragment_id,{} AS entity FROM {} WHERE {}=ANY(%s::uuid[])"
                ).format(
                    sql.Identifier(entity + "_id"),
                    sql.Identifier("kb", table),
                    sql.Identifier(entity + "_id"),
                ),
                (sets.get(entity, []),),
            )
            for p in cur.fetchall():
                if p["fragment_id"] in sets.get("fragment", []):
                    add(
                        "fragment",
                        p["fragment_id"],
                        entity,
                        p["entity"],
                        "grounds",
                        "recorded_provenance",
                        dict(p),
                    )
                elif p["fragment_id"] is None and p["source_id"] in sets.get("source", []):
                    add(
                        "source",
                        p["source_id"],
                        entity,
                        p["entity"],
                        "grounds",
                        "recorded_source_reference",
                        dict(p),
                    )
        cur.execute(
            "SELECT id,rule_id,rule_version_no,state FROM kb.use_record WHERE id=ANY(%s::uuid[]) "
            "AND principal_id=%s",
            (sets.get("use", []), principal.principal_id),
        )
        for u in cur.fetchall():
            if u["rule_id"] in sets.get("rule", []):
                add(
                    "rule",
                    u["rule_id"],
                    "use",
                    u["id"],
                    "retrieved" if u["state"] == "retrieved" else "used",
                    "explicit_usage_event",
                    dict(u),
                )
        cur.execute(
            "SELECT id,use_id FROM kb.outcome WHERE id=ANY(%s::uuid[])", (sets.get("outcome", []),)
        )
        for o in cur.fetchall():
            if o["use_id"] in sets.get("use", []):
                add("use", o["use_id"], "outcome", o["id"], "reports", "explicit_outcome", dict(o))
        return {
            "nodes": nodes,
            "edges": edges,
            "truncated": len(rows) > limit,
            "mode": "provenance",
            "capabilities": {
                "can_create": access["rank"] >= 20,
                "can_review": access["rank"] >= 30,
            },
        }
