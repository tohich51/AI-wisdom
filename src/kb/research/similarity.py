"""Derived concept embeddings and an undirected, non-evidentiary similarity layer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.access.policy import AccessDenied, transaction_identity
from kb.research.grounding import check_snapshot, validate_citation
from kb.retrieval.embedding_profile import SELECTED_PROFILE

TASK_NAME = "kb.research.similarity"
MAX_CONCEPTS = 200
EMBED_BATCH_SIZE = 16
PROVIDER_DEADLINE_SECONDS = 120
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

if (
    SELECTED_PROFILE.name != "openrouter:qwen3-embedding-8b:4096:v1"
    or SELECTED_PROFILE.model != "qwen/qwen3-embedding-8b"
    or SELECTED_PROFILE.api_base != "https://openrouter.ai/api/v1"
    or SELECTED_PROFILE.dimension != 4096
):
    raise RuntimeError("selected_embedding_profile_mismatch")


def concept_text(label: str, body: str) -> str:
    if not isinstance(label, str) or not isinstance(body, str):
        raise ValueError("invalid_concept")
    return f"{label}\n{body}"


def concept_text_hash(label: str, body: str) -> str:
    return hashlib.sha256(concept_text(label, body).encode("utf-8")).hexdigest()


def _visible_capture_fingerprint(concept_hashes) -> str:
    payload = json.dumps(
        sorted(
            (str(concept_id), current_text_hash) for concept_id, current_text_hash in concept_hashes
        ),
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _require_external_access(cur, principal, library_id: UUID) -> None:
    cur.execute(
        """SELECT l.organisation_id,
          kb.role_rank(kb.effective_role(kb.current_principal(),l.id)) AS rank,
          kb.library_policy_allows(l.id,'external') AS external_allowed,
          kb.library_policy_allows(l.id,'cloud') AS cloud_allowed
          FROM kb.library l WHERE l.id=%s""",
        (library_id,),
    )
    row = cur.fetchone()
    if (
        row is None
        or row["organisation_id"] != principal.account_id
        or row["rank"] is None
        or row["rank"] < 20
        or row["external_allowed"] is not True
        or (principal.cloud_client and row["cloud_allowed"] is not True)
    ):
        raise AccessDenied("forbidden")


def _validate_concept_dependencies(cur, principal, library_id: UUID, concepts: list[dict]) -> None:
    source_pins: dict[str, str] = {}
    for concept in concepts:
        manifest = concept["manifest"]
        citations = concept["citations"]
        if not isinstance(citations, list):
            raise ValueError("invalid_concept_dependencies")
        if manifest is None:
            if citations:
                raise ValueError("invalid_concept_dependencies")
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), list):
            raise ValueError("invalid_concept_dependencies")
        check_snapshot(cur, manifest)
        for citation in citations:
            validate_citation(cur, manifest, citation)
        for source in manifest["sources"]:
            source_id = source["source_id"]
            content_hash = source["content_hash"]
            previous = source_pins.get(source_id)
            if previous is not None and previous != content_hash:
                raise AccessDenied("forbidden")
            source_pins[source_id] = content_hash

    if not source_pins:
        return
    source_ids = list(source_pins)
    cur.execute(
        """SELECT s.id,s.library_id,s.publication,s.processing,s.content_hash,
          kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id)) AS rank,
          kb.library_policy_allows(s.library_id,'external') AS external_allowed,
          kb.library_policy_allows(s.library_id,'cloud') AS cloud_allowed,
          l.organisation_id
          FROM kb.source s JOIN kb.library l ON l.id=s.library_id
          WHERE s.id=ANY(%s::uuid[]) ORDER BY s.id""",
        (source_ids,),
    )
    rows = {str(row["id"]): row for row in cur.fetchall()}
    for source_id, content_hash in source_pins.items():
        row = rows.get(source_id)
        if (
            row is None
            or row["organisation_id"] != principal.account_id
            or row["publication"] == "withdrawn"
            or row["processing"] not in ("done", "partial")
            or row["content_hash"] != content_hash
            or row["rank"] is None
            or row["rank"] < 20
            or row["external_allowed"] is not True
            or (principal.cloud_client and row["cloud_allowed"] is not True)
        ):
            raise AccessDenied("forbidden")


def _visible_concepts(cur, principal, library_id: UUID) -> list[dict]:
    _require_external_access(cur, principal, library_id)
    cur.execute(
        """SELECT id,label,body,manifest,citations
          FROM kb.semantic_concept
          WHERE library_id=%s AND publication<>'withdrawn'
          ORDER BY created_at,id LIMIT %s""",
        (library_id, MAX_CONCEPTS + 1),
    )
    concepts = cur.fetchall()[:MAX_CONCEPTS]
    _validate_concept_dependencies(cur, principal, library_id, concepts)
    return concepts


def _normalise_vector(value: object, dimension: int = SELECTED_PROFILE.dimension) -> list[float]:
    if not isinstance(value, list) or len(value) != dimension:
        raise ValueError("malformed_embedding_dimension")
    vector: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError("malformed_embedding_value")
        number = float(component)
        if not math.isfinite(number) or abs(number) > 1e100:
            raise ValueError("malformed_embedding_value")
        vector.append(number)
    try:
        norm = math.sqrt(math.fsum(component * component for component in vector))
    except (OverflowError, ValueError) as exc:
        raise ValueError("malformed_embedding_value") from exc
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("zero_embedding_vector")
    return [component / norm for component in vector]


def _validate_embedding_response(payload: object, expected: int) -> list[list[float]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("malformed_embedding_response")
    model = payload.get("model")
    if not isinstance(model, str) or model not in (
        SELECTED_PROFILE.model,
        "Qwen/Qwen3-Embedding-8B",
    ):
        raise ValueError("embedding_provider_model_mismatch")
    items = payload["data"]
    if len(items) != expected:
        raise ValueError("malformed_embedding_count")
    ordered: dict[int, list[float]] = {}
    for item in items:
        if not isinstance(item, dict) or type(item.get("index")) is not int:
            raise ValueError("malformed_embedding_index")
        index = item["index"]
        if index < 0 or index >= expected or index in ordered:
            raise ValueError("malformed_embedding_index")
        ordered[index] = _normalise_vector(item.get("embedding"))
    if set(ordered) != set(range(expected)):
        raise ValueError("malformed_embedding_index")
    return [ordered[index] for index in range(expected)]


def _read_embedding_key() -> str:
    value = os.environ.get("KB_CONCEPT_EMBEDDING_KEY_FILE")
    if not value:
        raise ValueError("embedding_key_unavailable")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("insecure_embedding_key")

    current = path
    first = True
    while True:
        try:
            metadata = os.lstat(current)
        except OSError as exc:
            raise ValueError("embedding_key_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("insecure_embedding_key")
        if first:
            file_metadata = metadata
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise ValueError("insecure_embedding_key")
            if metadata.st_uid != os.geteuid():
                raise ValueError("insecure_embedding_key")
        elif metadata.st_mode & 0o022:
            raise ValueError("insecure_embedding_key")
        parent = current.parent
        if parent == current:
            break
        current = parent
        first = False

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_mode & 0o077
                or opened.st_uid != os.geteuid()
                or (opened.st_dev, opened.st_ino) != (file_metadata.st_dev, file_metadata.st_ino)
            ):
                raise ValueError("insecure_embedding_key")
            raw = os.read(descriptor, 8193)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValueError("embedding_key_unavailable") from exc
    if len(raw) > 8192:
        raise ValueError("insecure_embedding_key")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if not raw or any(byte <= 32 or byte >= 127 for byte in raw):
        raise ValueError("insecure_embedding_key")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("insecure_embedding_key") from exc


def _openrouter_embeddings(texts: list[str], key: str, deadline: float) -> list[list[float]]:
    if not 1 <= len(texts) <= EMBED_BATCH_SIZE:
        raise ValueError("invalid_embedding_batch")

    async def _request_embeddings() -> list[list[float]]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("embedding_deadline_exceeded")

        async with asyncio.timeout(remaining):
            timeout = httpx.Timeout(
                timeout=remaining,
                connect=min(5.0, remaining),
                read=min(20.0, remaining),
                write=min(10.0, remaining),
                pool=min(5.0, remaining),
            )
            limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
            body = bytearray()
            async with httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    SELECTED_PROFILE.api_base + "/embeddings",
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    json={
                        "model": SELECTED_PROFILE.model,
                        "input": texts,
                        "encoding_format": "float",
                        "dimensions": SELECTED_PROFILE.dimension,
                    },
                ) as response:
                    if response.status_code != 200:
                        raise ValueError("embedding_provider_failed")
                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        if not content_length.isdigit() or int(content_length) > MAX_RESPONSE_BYTES:
                            raise ValueError("embedding_response_too_large")
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError("embedding_response_too_large")
                        body.extend(chunk)
                        if time.monotonic() >= deadline:
                            raise TimeoutError("embedding_deadline_exceeded")
            if time.monotonic() >= deadline:
                raise TimeoutError("embedding_deadline_exceeded")
            try:
                payload = json.loads(bytes(body))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("malformed_embedding_response") from exc
            return _validate_embedding_response(payload, len(texts))

    try:
        return asyncio.run(_request_embeddings())
    except TimeoutError:
        raise TimeoutError("embedding_deadline_exceeded") from None
    except httpx.TimeoutException:
        raise TimeoutError("embedding_provider_timeout") from None
    except httpx.HTTPError:
        raise RuntimeError("embedding_provider_unavailable") from None


def _embed_missing(
    texts: list[str],
    *,
    embedder=None,
    deadline: float | None = None,
) -> list[list[float]]:
    if not texts:
        return []
    if deadline is None:
        deadline = time.monotonic() + PROVIDER_DEADLINE_SECONDS
    key = _read_embedding_key() if embedder is None else None
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), EMBED_BATCH_SIZE):
        if time.monotonic() >= deadline:
            raise TimeoutError("embedding_deadline_exceeded")
        batch = texts[offset : offset + EMBED_BATCH_SIZE]
        if len(batch) > EMBED_BATCH_SIZE:
            raise ValueError("invalid_embedding_batch")
        if embedder is None:
            if key is None:
                raise RuntimeError("embedding_key_missing")
            batch_vectors = _openrouter_embeddings(batch, key, deadline)
        else:
            payload = embedder(batch)
            batch_vectors = _validate_embedding_response(
                {
                    "model": SELECTED_PROFILE.model,
                    "data": [
                        {"index": index, "embedding": vector}
                        for index, vector in enumerate(payload)
                    ],
                }
                if isinstance(payload, list)
                else payload,
                len(batch),
            )
        vectors.extend(batch_vectors)
    return vectors


def _cached_vectors(cur, library_id: UUID, text_hashes: list[str]) -> dict[str, list[float]]:
    if not text_hashes:
        return {}
    cur.execute(
        """SELECT DISTINCT ON (e.text_hash) e.text_hash,e.embedding
          FROM kb.concept_embedding e
          WHERE e.library_id=%s AND e.profile_hash=%s
            AND e.text_hash=ANY(%s::text[])
          ORDER BY e.text_hash,e.created_at,e.concept_id""",
        (library_id, SELECTED_PROFILE.profile_hash, text_hashes),
    )
    return {row["text_hash"]: _normalise_vector(row["embedding"]) for row in cur.fetchall()}


def _rank_similarity(
    concepts: list[dict], vectors: dict[str, list[float]], profile_hash: str
) -> list[dict]:
    node_ids = sorted((concept["id"] for concept in concepts), key=str)
    hashes = {concept["id"]: concept["text_hash"] for concept in concepts}
    neighbours: dict[UUID, list[tuple[float, UUID]]] = {node_id: [] for node_id in node_ids}
    pairs: dict[tuple[UUID, UUID], float] = {}

    for left_index, left in enumerate(node_ids):
        left_vector = vectors.get(hashes[left])
        if left_vector is None:
            raise ValueError("missing_embedding")
        for right in node_ids[left_index + 1 :]:
            right_vector = vectors.get(hashes[right])
            if right_vector is None:
                raise ValueError("missing_embedding")
            score = math.fsum(a * b for a, b in zip(left_vector, right_vector, strict=True))
            score = max(-1.0, min(1.0, float(score)))
            if score < 0.65:
                continue
            pairs[(left, right)] = score
            neighbours[left].append((score, right))
            neighbours[right].append((score, left))

    selected: dict[tuple[UUID, UUID], float] = {}
    for node_id in node_ids:
        for score, neighbour in sorted(
            neighbours[node_id], key=lambda item: (-item[0], str(item[1]))
        )[:3]:
            pair = tuple(sorted((node_id, neighbour)))
            selected[pair] = score

    edges = []
    for (source, target), score in sorted(
        selected.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
    ):
        identity = (
            f"kb-concept-similarity:{profile_hash}:{source}:{hashes[source]}:"
            f"{target}:{hashes[target]}"
        )
        edges.append(
            {
                "id": uuid5(NAMESPACE_URL, identity),
                "source": source,
                "target": target,
                "source_text_hash": hashes[source],
                "target_text_hash": hashes[target],
                "score": score,
            }
        )
    return edges


def _assert_capture_current(cur, principal, library_id: UUID, captured: list[dict]) -> None:
    current_rows = _visible_concepts(cur, principal, library_id)
    current = {row["id"]: row for row in current_rows}
    for item in captured:
        row = current.get(item["id"])
        if (
            row is None
            or concept_text_hash(row["label"], row["body"]) != item["text_hash"]
            or row["manifest"] != item["manifest"]
            or row["citations"] != item["citations"]
        ):
            raise AccessDenied("forbidden")


@contextmanager
def _library_execution_lock(conn, library_id: UUID):
    key = f"concept-similarity-execution:{library_id}"
    conn.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (key,))
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        try:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
            conn.commit()
        except psycopg.Error:
            conn.rollback()


def rebuild_similarity(dsn, principal, library_id, *, embedder=None) -> dict:
    library_id = UUID(str(library_id))
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with _library_execution_lock(conn, library_id):
            with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
                concepts = _visible_concepts(cur, principal, library_id)
                if not concepts:
                    return {"status": "no_concepts", "ready": 0, "edges": 0}
                captured = [
                    {
                        "id": row["id"],
                        "label": row["label"],
                        "body": row["body"],
                        "text_hash": concept_text_hash(row["label"], row["body"]),
                        "embedding_text": concept_text(row["label"], row["body"]),
                        "manifest": row["manifest"],
                        "citations": row["citations"],
                    }
                    for row in concepts
                ]
                selection_hash = _visible_capture_fingerprint(
                    (item["id"], item["text_hash"]) for item in captured
                )
                text_hashes = list(dict.fromkeys(item["text_hash"] for item in captured))
                cached = _cached_vectors(cur, library_id, text_hashes)
            conn.commit()

            missing = [text_hash for text_hash in text_hashes if text_hash not in cached]
            embedding_text_by_hash = {
                item["text_hash"]: item["embedding_text"] for item in captured
            }
            deadline = time.monotonic() + PROVIDER_DEADLINE_SECONDS
            computed = _embed_missing(
                [embedding_text_by_hash[text_hash] for text_hash in missing],
                embedder=embedder,
                deadline=deadline,
            )
            vectors = {**cached, **dict(zip(missing, computed, strict=True))}
            edges = _rank_similarity(captured, vectors, SELECTED_PROFILE.profile_hash)

            with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
                current_concepts = _visible_concepts(cur, principal, library_id)
                current_capture = [
                    (row["id"], concept_text_hash(row["label"], row["body"]))
                    for row in current_concepts
                ]
                if _visible_capture_fingerprint(current_capture) != selection_hash:
                    raise AccessDenied("forbidden")
                current_by_id = {row["id"]: row for row in current_concepts}
                for item in captured:
                    row = current_by_id.get(item["id"])
                    if (
                        row is None
                        or row["manifest"] != item["manifest"]
                        or row["citations"] != item["citations"]
                    ):
                        raise AccessDenied("forbidden")
                cur.executemany(
                    """INSERT INTO kb.concept_embedding(
                        concept_id,library_id,profile_hash,text_hash,embedding
                      ) VALUES(%s,%s,%s,%s,%s)
                      ON CONFLICT(concept_id,profile_hash,text_hash) DO NOTHING""",
                    [
                        (
                            item["id"],
                            library_id,
                            SELECTED_PROFILE.profile_hash,
                            item["text_hash"],
                            vectors[item["text_hash"]],
                        )
                        for item in captured
                    ],
                )
                cur.executemany(
                    """INSERT INTO kb.concept_similarity(
                        id,library_id,profile_hash,source_concept,target_concept,
                        source_text_hash,target_text_hash,score
                      ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT DO NOTHING""",
                    [
                        (
                            edge["id"],
                            library_id,
                            SELECTED_PROFILE.profile_hash,
                            edge["source"],
                            edge["target"],
                            edge["source_text_hash"],
                            edge["target_text_hash"],
                            edge["score"],
                        )
                        for edge in edges
                    ],
                )
                cur.execute(
                    """INSERT INTO kb.concept_similarity_build(
                        library_id,profile_hash,selection_hash,concept_ids,edge_ids
                      ) VALUES(%s,%s,%s,%s::uuid[],%s::uuid[])
                      ON CONFLICT(library_id,profile_hash,selection_hash) DO NOTHING""",
                    (
                        library_id,
                        SELECTED_PROFILE.profile_hash,
                        selection_hash,
                        sorted((item["id"] for item in captured), key=str),
                        sorted((edge["id"] for edge in edges), key=str),
                    ),
                )
            conn.commit()
            return {"status": "ready", "ready": len(captured), "edges": len(edges)}


def _has_pending_similarity(cur, principal, library_id: UUID) -> bool:
    cur.execute(
        """SELECT n.nspname
          FROM pg_catalog.pg_class c
          JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
          WHERE c.relname='procrastinate_jobs' AND c.relkind='r'
          ORDER BY (n.nspname=current_schema()) DESC LIMIT 1"""
    )
    table = cur.fetchone()
    if table is None:
        return False
    schema = table["nspname"]
    query = sql.SQL(
        """SELECT 1 FROM {}.{}
          WHERE task_name=%s
            AND args->>'library_id'=%s
            AND args->>'principal_id'=%s
            AND args->>'account_id'=%s
            AND status::text=ANY(%s::text[])
          LIMIT 1"""
    ).format(sql.Identifier(schema), sql.Identifier("procrastinate_jobs"))
    cur.execute(
        query,
        (
            TASK_NAME,
            str(library_id),
            str(principal.principal_id),
            str(principal.account_id),
            ["todo", "queued", "scheduled", "doing", "running", "in-progress", "in_progress"],
        ),
    )
    return cur.fetchone() is not None


def enqueue_similarity_on_transaction(cur, principal, library_id: UUID) -> dict:
    library_id = UUID(str(library_id))
    concepts = _visible_concepts(cur, principal, library_id)
    if not concepts:
        return {"status": "no_concepts", "library_id": library_id}

    queueing_lock = f"concept-similarity:{principal.principal_id}:{library_id}"
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (queueing_lock,))
    if _has_pending_similarity(cur, principal, library_id):
        return {"status": "already_queued", "library_id": library_id}

    import procrastinate

    from kb.jobs.research import register_similarity_task

    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=""))
    task = register_similarity_task(app, "")
    task.configure(connection=cur.connection, lock=queueing_lock).defer(
        library_id=str(library_id),
        principal_id=str(principal.principal_id),
        account_id=str(principal.account_id),
        cloud_client=principal.cloud_client,
    )
    return {"status": "queued", "library_id": library_id}


def enqueue_similarity(conn, principal, library_id: UUID) -> dict:
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        return enqueue_similarity_on_transaction(cur, principal, library_id)


def similarity_layer(
    cur, principal, library_id: UUID, node_hashes: dict[UUID, str], limit: int
) -> dict:
    profile_hash = SELECTED_PROFILE.profile_hash
    total = len(node_hashes)
    if total == 0:
        return {
            "available": False,
            "reason": "no_visible_concepts",
            "profile_hash": profile_hash,
            "ready": 0,
            "total": 0,
            "edges": [],
            "truncated": False,
        }

    selection_hash = _visible_capture_fingerprint(node_hashes.items())
    wanted = [
        {"concept_id": str(concept_id), "text_hash": text_hash}
        for concept_id, text_hash in node_hashes.items()
    ]
    cur.execute(
        """SELECT e.concept_id
          FROM kb.concept_embedding e
          JOIN jsonb_to_recordset(%s::jsonb)
            AS wanted(concept_id uuid,text_hash text)
            ON e.concept_id=wanted.concept_id
           AND e.text_hash=wanted.text_hash
          WHERE e.library_id=%s AND e.profile_hash=%s""",
        (Jsonb(wanted), library_id, profile_hash),
    )
    ready_ids = {row["concept_id"] for row in cur.fetchall()}
    cur.execute(
        """SELECT b.edge_ids
          FROM kb.concept_similarity_build b
          WHERE b.library_id=%s AND b.profile_hash=%s
            AND b.selection_hash=%s""",
        (library_id, profile_hash, selection_hash),
    )
    build = cur.fetchone()

    edges: list[dict] = []
    truncated = False
    edge_limit = min(max(limit, 1) * 3, 1500)
    if build is not None and ready_ids:
        cur.execute(
            """SELECT s.id,s.source_concept,s.target_concept,s.score
              FROM unnest(%s::uuid[]) AS selected(edge_id)
              JOIN kb.concept_similarity s ON s.id=selected.edge_id
              JOIN jsonb_to_recordset(%s::jsonb)
                AS wanted(concept_id uuid,text_hash text)
                ON wanted.concept_id=s.source_concept
              JOIN jsonb_to_recordset(%s::jsonb)
                AS wanted_target(concept_id uuid,text_hash text)
                ON wanted_target.concept_id=s.target_concept
              WHERE s.library_id=%s AND s.profile_hash=%s
                AND s.source_concept=ANY(%s::uuid[])
                AND s.target_concept=ANY(%s::uuid[])
                AND s.source_text_hash=wanted.text_hash
                AND s.target_text_hash=wanted_target.text_hash
              ORDER BY s.source_concept,s.target_concept
              LIMIT %s""",
            (
                build["edge_ids"],
                Jsonb(wanted),
                Jsonb(wanted),
                library_id,
                profile_hash,
                list(ready_ids),
                list(ready_ids),
                edge_limit + 1,
            ),
        )
        rows = cur.fetchall()
        edges = [
            {
                "id": str(row["id"]),
                "source": "concept:" + str(row["source_concept"]),
                "target": "concept:" + str(row["target_concept"]),
                "score": float(row["score"]),
                "symmetric": True,
            }
            for row in rows[:edge_limit]
        ]
        truncated = len(rows) > edge_limit

    ready = len(ready_ids)
    complete = build is not None and ready == total
    return {
        "available": complete,
        "reason": None if complete else "concept_similarity_cache_incomplete",
        "profile_hash": profile_hash,
        "ready": ready,
        "total": total,
        "edges": edges,
        "truncated": truncated,
    }
