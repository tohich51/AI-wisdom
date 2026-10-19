"""Profile-bound, checkpointed generations of published knowledge only."""

import hashlib
from dataclasses import dataclass
from uuid import UUID

from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.retrieval.embedding_profile import SELECTED_PROFILE


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Document:
    id: UUID
    statement: str

    @property
    def statement_hash(self) -> str:
        return sha256(self.statement)


@dataclass(frozen=True)
class Snapshot:
    library_id: UUID
    revision: int
    documents: tuple[Document, ...]

    @property
    def manifest_hash(self) -> str:
        return sha256(
            "\n".join(
                f"{d.id}:{d.statement_hash}"
                for d in sorted(self.documents, key=lambda item: item.id)
            )
        )

    @property
    def content_hash(self) -> str:
        return sha256(f"{SELECTED_PROFILE.profile_hash}:{self.revision}:{self.manifest_hash}")


@dataclass(frozen=True)
class Build:
    snapshot: Snapshot
    generation: int
    already_current: bool = False

    @property
    def resource_root(self) -> str:
        return f"viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-{self.generation}"

    def document_uri(self, document: Document) -> str:
        return f"{self.resource_root}/{document.id}.md"


def resolve_context(conn, library_id: UUID) -> Principal | None:
    row = conn.execute(
        "SELECT principal_id, account_id FROM kb.semantic_index_context(%s, %s, 1)",
        (SELECTED_PROFILE.profile_hash, library_id),
    ).fetchone()
    if row is None:
        return None
    return Principal(row[0], row[1])


def capture(conn, principal: Principal, library_id: UUID) -> Snapshot:
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,91))", (str(library_id),))
        cur.execute(
            "SELECT generation FROM kb.library WHERE id=%s AND organisation_id=%s "
            "AND kb.library_policy_allows(id,'external') "
            "AND kb.role_rank(kb.effective_role(kb.current_principal(),id))>=40 "
            "AND EXISTS(SELECT 1 FROM kb.membership m "
            "WHERE m.organisation_id=kb.library.organisation_id "
            "AND m.principal_id=kb.current_principal() AND m.status='active')",
            (library_id, principal.account_id),
        )
        library = cur.fetchone()
        if not library:
            raise AccessDenied("forbidden")
        cur.execute(
            "SELECT id,statement FROM kb.knowledge WHERE library_id=%s "
            "AND publication='published' AND NOT EXISTS(SELECT 1 FROM kb.knowledge_provenance p "
            "JOIN kb.source s ON s.id=p.source_id WHERE p.knowledge_id=kb.knowledge.id "
            "AND s.publication='withdrawn') ORDER BY id",
            (library_id,),
        )
        documents = tuple(Document(row["id"], row["statement"]) for row in cur.fetchall())
    return Snapshot(library_id, library["generation"], documents)


def begin(conn, principal: Principal, snapshot: Snapshot, *, force=False) -> Build:
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,91))", (str(snapshot.library_id),)
        )
        cur.execute(
            "SELECT 1 FROM kb.index_account WHERE library_id=%s AND state='ready' "
            "AND profile_hash=%s AND dimension=%s AND embedding_profile=%s",
            (
                snapshot.library_id,
                SELECTED_PROFILE.profile_hash,
                SELECTED_PROFILE.dimension,
                SELECTED_PROFILE.name,
            ),
        )
        if not cur.fetchone():
            raise ValueError("projection_profile_mismatch")
        cur.execute(
            "SELECT generation,state FROM kb.index_generation WHERE library_id=%s "
            "AND content_hash=%s AND profile_hash=%s AND state IN ('building','current') "
            "ORDER BY generation DESC LIMIT 1",
            (snapshot.library_id, snapshot.content_hash, SELECTED_PROFILE.profile_hash),
        )
        existing = cur.fetchone()
        if existing:
            return Build(snapshot, existing["generation"], existing["state"] == "current")
        cur.execute(
            "UPDATE kb.index_generation SET state='retired',updated_at=now(),finished_at=now() "
            "WHERE library_id=%s AND state='building'",
            (snapshot.library_id,),
        )
        cur.execute(
            "SELECT coalesce(max(generation),0)+1 AS n FROM kb.index_generation "
            "WHERE library_id=%s",
            (snapshot.library_id,),
        )
        generation = cur.fetchone()["n"]
        build = Build(snapshot, generation)
        cur.execute(
            "INSERT INTO kb.index_generation(library_id,generation,state,content_hash,"
            "started_by,profile_hash,indexed_revision,manifest_hash,resource_root,"
            "indexed_documents) "
            "VALUES(%s,%s,'building',%s,%s,%s,%s,%s,%s,%s)",
            (
                snapshot.library_id,
                generation,
                snapshot.content_hash,
                principal.principal_id,
                SELECTED_PROFILE.profile_hash,
                snapshot.revision,
                snapshot.manifest_hash,
                build.resource_root,
                len(snapshot.documents),
            ),
        )
    return build


def completed_documents(conn, principal: Principal, build: Build) -> dict[UUID, str]:
    with transaction_identity(conn, principal):
        rows = conn.execute(
            "SELECT knowledge_id,statement_hash FROM kb.index_document "
            "WHERE library_id=%s AND generation=%s",
            (build.snapshot.library_id, build.generation),
        ).fetchall()
    return dict(rows)


def previous_documents(
    conn, principal: Principal, build: Build
) -> tuple[str | None, dict[UUID, str]]:
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,91))",
            (str(build.snapshot.library_id),),
        )
        cur.execute(
            "SELECT generation,resource_root FROM kb.index_generation "
            "WHERE library_id=%s AND profile_hash=%s AND state='current' "
            "AND verified_at IS NOT NULL AND generation<>%s "
            "AND EXISTS(SELECT 1 FROM kb.library l JOIN kb.membership m "
            "ON m.organisation_id=l.organisation_id "
            "WHERE l.id=kb.index_generation.library_id AND l.organisation_id=%s "
            "AND m.principal_id=kb.current_principal() AND m.status='active') "
            "ORDER BY generation DESC LIMIT 1",
            (
                build.snapshot.library_id,
                SELECTED_PROFILE.profile_hash,
                build.generation,
                principal.account_id,
            ),
        )
        source = cur.fetchone()
        if source is None:
            return None, {}
        source_root = (
            f"viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-{source['generation']}"
        )
        if source["resource_root"] != source_root:
            raise ValueError("projection_source_root_mismatch")
        cur.execute(
            "SELECT knowledge_id,statement_hash FROM kb.index_document "
            "WHERE library_id=%s AND generation=%s",
            (build.snapshot.library_id, source["generation"]),
        )
        hashes = {row["knowledge_id"]: row["statement_hash"] for row in cur.fetchall()}
    return source_root, hashes


def checkpoint(conn, principal: Principal, build: Build, document: Document) -> None:
    with transaction_identity(conn, principal):
        conn.execute(
            "INSERT INTO kb.index_document(library_id,generation,knowledge_id,"
            "resource_uri,statement_hash) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(library_id,generation,knowledge_id) DO NOTHING",
            (
                build.snapshot.library_id,
                build.generation,
                document.id,
                build.document_uri(document),
                document.statement_hash,
            ),
        )


def activate(conn, principal: Principal, build: Build) -> None:
    # The database trigger rechecks revision/profile, all document hashes and manifest.
    # Both updates roll back if that final check refuses activation.
    with transaction_identity(conn, principal):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,91))",
            (str(build.snapshot.library_id),),
        )
        conn.execute(
            "UPDATE kb.index_generation SET state='retired',updated_at=now(),finished_at=now() "
            "WHERE library_id=%s AND state='current'",
            (build.snapshot.library_id,),
        )
        row = conn.execute(
            "UPDATE kb.index_generation SET state='current',verified_at=now(),"
            "finished_at=now(),updated_at=now() WHERE library_id=%s AND generation=%s "
            "AND state='building' RETURNING generation",
            (build.snapshot.library_id, build.generation),
        ).fetchone()
        if not row:
            raise ValueError("projection_not_building")


def fail(conn, principal: Principal, build: Build) -> None:
    with transaction_identity(conn, principal):
        row = conn.execute(
            "SELECT kb.cancel_semantic_build(%s,%s)", (build.snapshot.library_id, build.generation)
        ).fetchone()
        if not row or not row[0]:
            raise ValueError("projection_cancellation_refused")


def retire_unavailable_builds(conn) -> int:
    row = conn.execute(
        "SELECT kb.retire_unavailable_semantic_builds(%s)",
        (SELECTED_PROFILE.profile_hash,),
    ).fetchone()
    return row[0]


def pending_libraries(conn, *, rotation: int = 0) -> list[UUID]:
    rows = conn.execute(
        "SELECT library_id FROM kb.semantic_index_context(%s, NULL, 1000, %s)",
        (SELECTED_PROFILE.profile_hash, rotation),
    ).fetchall()
    return [row[0] for row in rows]
