"""Read-only vector candidates, with canonical PostgreSQL hydration and final ACL checks."""

from dataclasses import dataclass
from uuid import UUID

from psycopg.rows import dict_row

from kb.access.policy import Principal, transaction_identity
from kb.catalog.content import attach_knowledge_provenance, lexical_search_knowledge
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.ranking import reciprocal_rank_fusion
from kb.retrieval.semantic_registry import sha256
from kb.retrieval.vector_reader import IndexUnavailable, OpenVikingReader


@dataclass(frozen=True)
class Plan:
    library_id: UUID
    revision: int
    generation: int | None
    identity: str | None
    root: str | None
    usable: bool


def plans(conn, principal, scopes=None) -> dict[UUID, Plan]:
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT l.id,l.generation AS revision,a.read_identity,g.generation,g.resource_root,"
            "(a.state='ready' AND a.profile_hash=%s AND a.dimension=%s "
            "AND a.embedding_profile=%s AND g.profile_hash=%s AND g.verified_at IS NOT NULL "
            "AND g.indexed_revision=l.generation "
            "AND kb.library_policy_allows(l.id,'external')) AS usable "
            "FROM kb.library l LEFT JOIN kb.index_account a ON a.library_id=l.id "
            "LEFT JOIN kb.index_generation g ON g.library_id=l.id AND g.state='current' "
            "WHERE l.organisation_id=%s "
            "AND EXISTS(SELECT 1 FROM kb.membership m WHERE m.organisation_id=l.organisation_id "
            "AND m.principal_id=kb.current_principal() AND m.status='active') "
            "AND kb.role_rank(kb.effective_role(kb.current_principal(),l.id))>=10 "
            "AND (%s::uuid[] IS NULL OR l.id=ANY(%s::uuid[])) ORDER BY l.id",
            (
                SELECTED_PROFILE.profile_hash,
                SELECTED_PROFILE.dimension,
                SELECTED_PROFILE.name,
                SELECTED_PROFILE.profile_hash,
                principal.account_id,
                list(scopes) if scopes is not None else None,
                list(scopes) if scopes is not None else None,
            ),
        )
        return {
            r["id"]: Plan(
                r["id"],
                r["revision"],
                r["generation"],
                r["read_identity"],
                r["resource_root"],
                bool(r["usable"]),
            )
            for r in cur.fetchall()
        }


class SemanticSearch:
    def __init__(self, reader: OpenVikingReader):
        self.reader = reader

    def search(self, conn, principal: Principal, query, scopes=None, limit=10, *, source_ids=None):
        initial = plans(conn, principal, scopes)
        candidates = {}
        attempted = {}
        unavailable = set()
        # Bound upstream fan-out. Libraries outside this budget remain explicitly lexical.
        for plan in [p for p in initial.values() if p.usable][:20]:
            if plan.identity is None or plan.root is None:
                continue
            if plans(conn, principal, [plan.library_id]).get(plan.library_id) != plan:
                continue
            attempted[plan.library_id] = plan
            try:
                candidates[plan.library_id] = self.reader.find(
                    plan.identity,
                    plan.root,
                    query,
                    100 if source_ids is not None else min(100, limit * 3),
                )
            except IndexUnavailable:
                unavailable.add(plan.library_id)
        fresh = plans(conn, principal, scopes)
        fallback = {
            lib
            for lib, plan in fresh.items()
            if not plan.usable or attempted.get(lib) != plan or lib in unavailable
        }
        hits = []
        for lib, items in candidates.items():
            refreshed_plan = fresh.get(lib)
            if refreshed_plan is None or lib in fallback or refreshed_plan != attempted.get(lib):
                continue
            scores = {item.uri: item.score for item in items}
            if not scores:
                continue
            with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT k.*,d.resource_uri FROM kb.index_document d "
                    "JOIN kb.knowledge k ON k.id=d.knowledge_id AND k.library_id=d.library_id "
                    "JOIN kb.library l ON l.id=k.library_id "
                    "JOIN kb.index_generation g ON g.library_id=d.library_id "
                    "AND g.generation=d.generation AND g.state='current' "
                    "JOIN kb.index_account a ON a.library_id=l.id "
                    "WHERE d.library_id=%s AND d.generation=%s AND d.resource_uri=ANY(%s::text[]) "
                    "AND l.organisation_id=%s AND l.generation=%s "
                    "AND g.indexed_revision=l.generation "
                    "AND g.profile_hash=%s AND a.profile_hash=g.profile_hash AND a.state='ready' "
                    "AND a.dimension=%s AND g.verified_at IS NOT NULL "
                    "AND k.publication='published' "
                    "AND d.statement_hash=encode(sha256(convert_to(k.statement,'UTF8')),'hex') "
                    "AND d.resource_uri=g.resource_root || '/' || k.id::text || '.md'",
                    (
                        lib,
                        refreshed_plan.generation,
                        list(scores),
                        principal.account_id,
                        refreshed_plan.revision,
                        SELECTED_PROFILE.profile_hash,
                        SELECTED_PROFILE.dimension,
                    ),
                )
                for row in cur.fetchall():
                    hit = dict(row)
                    hit["score"] = scores[hit.pop("resource_uri")]
                    hit["retrieval_mode"] = "semantic"
                    hits.append(hit)
        lexical = lexical_search_knowledge(
            conn, principal, query, list(fresh), min(50, limit * 3), source_ids=source_ids
        )
        for hit in lexical["hits"]:
            hit["retrieval_mode"] = "lexical"
            hits.append(hit)
        # Do not return candidates from a generation or membership that changed
        # during the external call or the lexical fallback. RLS also checks knowledge.
        final = plans(conn, principal, scopes)
        hits = [
            h
            for h in hits
            if h["library_id"] in final
            and (
                h["retrieval_mode"] == "lexical"
                or final[h["library_id"]] == attempted.get(h["library_id"])
            )
        ]
        with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
            if hits:
                cur.execute(
                    "SELECT k.*,l.generation AS live_revision,g.generation AS live_generation,"
                    "g.indexed_revision,g.profile_hash AS generation_profile,"
                    "a.profile_hash AS account_profile,"
                    "a.state AS account_state,a.dimension,g.verified_at,d.statement_hash,"
                    "d.resource_uri,"
                    "g.resource_root FROM kb.knowledge k JOIN kb.library l ON l.id=k.library_id "
                    "LEFT JOIN kb.index_generation g ON g.library_id=l.id AND g.state='current' "
                    "LEFT JOIN kb.index_account a ON a.library_id=l.id "
                    "LEFT JOIN kb.index_document d ON d.library_id=k.library_id "
                    "AND d.knowledge_id=k.id AND d.generation=g.generation "
                    "WHERE k.id=ANY(%s::uuid[]) AND k.publication<>'withdrawn' "
                    "AND l.organisation_id=%s AND EXISTS(SELECT 1 FROM kb.membership m "
                    "WHERE m.organisation_id=l.organisation_id "
                    "AND m.principal_id=kb.current_principal() "
                    "AND m.status='active') AND NOT EXISTS(SELECT 1 FROM kb.knowledge_provenance p "
                    "JOIN kb.source source ON source.id=p.source_id WHERE p.knowledge_id=k.id "
                    "AND source.publication='withdrawn')",
                    ([h["id"] for h in hits], principal.account_id),
                )
                rows = {r["id"]: r for r in cur.fetchall()}
                accepted = []
                for hit in hits:
                    row = rows.get(hit["id"])
                    if row is None or row["library_id"] != hit["library_id"]:
                        continue
                    if hit["retrieval_mode"] == "semantic":
                        plan = attempted[hit["library_id"]]
                        if not (
                            row["publication"] == "published"
                            and row["live_generation"] == plan.generation
                            and row["live_revision"] == row["indexed_revision"] == plan.revision
                            and row["generation_profile"]
                            == row["account_profile"]
                            == SELECTED_PROFILE.profile_hash
                            and row["account_state"] == "ready"
                            and row["dimension"] == SELECTED_PROFILE.dimension
                            and row["verified_at"] is not None
                            and row["statement_hash"] == sha256(row["statement"])
                            and plan.root is not None
                            and row["resource_uri"] == plan.root + "/" + str(row["id"]) + ".md"
                        ):
                            continue
                    # Canonical fields, score only from the verified candidate path.
                    for field in hit:
                        if field in row and field not in ("score", "retrieval_mode", "provenance"):
                            hit[field] = row[field]
                    accepted.append(hit)
                hits = accepted
                attach_knowledge_provenance(cur, hits)
                if source_ids is not None:
                    requested = set(source_ids)
                    hits = [
                        h for h in hits if any(p["source_id"] in requested for p in h["provenance"])
                    ]
        # Fuse only hydrated, finally authorized candidates. Each retrieval path
        # contributes a rank once; semantic similarity and FTS scores are not comparable.
        semantic_rank = sorted(
            (h for h in hits if h["retrieval_mode"] == "semantic"),
            key=lambda h: (-h["score"], str(h["id"])),
        )
        lexical_rank = sorted(
            (h for h in hits if h["retrieval_mode"] == "lexical"),
            key=lambda h: (-h["score"], str(h["id"])),
        )
        unique = {str(h["id"]): h for h in [*lexical_rank, *semantic_rank]}
        ranked = reciprocal_rank_fusion(
            [
                [str(h["id"]) for h in semantic_rank],
                [str(h["id"]) for h in lexical_rank],
            ]
        )
        hits = [{**unique[ident], "score": score} for ident, score in ranked[:limit]]
        pending = {
            lib
            for lib, p in final.items()
            if not p.usable or attempted.get(lib) != p or lib in unavailable
        }
        mode = (
            "hybrid" if semantic_rank and lexical_rank else ("lexical" if pending else "semantic")
        )
        return {
            "mode": mode,
            "semantic_search_enabled": True,
            "embedding_model": SELECTED_PROFILE.model,
            "hits": hits,
            "retrieved_only": True,
            "indexed_library_count": len(final) - len(pending),
            "pending_library_count": len(pending),
            "fallback_reason": "index_pending_or_unavailable" if pending else None,
        }
