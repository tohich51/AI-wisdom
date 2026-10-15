"""Durable ingestion on Procrastinate; identifiers in the queue, content in PostgreSQL."""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import procrastinate
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.storage import LocalBlobStore, _resolve_in_root
from kb.document_parser import NeedsOCR, ParseRejected, parse_document
from kb.model_runner import GenerateResponse

MODEL = "MiniMax-M3.1-Flash-Preview"
_LEASE_SECONDS = 480


class AttemptInProgress(RuntimeError):
    """Another worker still owns this job; redelivery must retry later."""


class _StaleAttempt(Exception):
    """A replacement worker has taken over, so this attempt must stop quietly."""


def build_queue(dsn: str):
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn))

    @app.task(
        name="kb.ingest_source",
        queue="ingestion",
        retry=procrastinate.RetryStrategy(max_attempts=6, wait=480),
    )
    def process(job_id: str, principal_id: str, account_id: str):
        run_job(
            dsn,
            UUID(job_id),
            Principal(UUID(principal_id), UUID(account_id)),
            Path(os.environ["KB_BLOB_ROOT"]),
            os.environ.get("KB_MODEL_RUNNER_URL"),
            os.environ.get("KB_WORKER_TOKEN"),
        )

    from kb.research_worker import register_research_task

    register_research_task(app, dsn)
    return app, process


def _admit(conn, principal, source_id, *, extraction=False):
    row = conn.execute(
        """SELECT s.*,kb.role_rank(kb.effective_role(%s,s.library_id)) AS rank,
      kb.is_org_admin(%s,%s) AS owner,
      kb.library_policy_allows(s.library_id,'generation') AS generation_allowed FROM kb.source s
      WHERE s.id=%s AND EXISTS(SELECT 1 FROM kb.membership m
        WHERE m.principal_id=%s AND m.organisation_id=%s AND m.status='active')""",
        (
            principal.principal_id,
            principal.account_id,
            principal.principal_id,
            source_id,
            principal.principal_id,
            principal.account_id,
        ),
    ).fetchone()
    if (
        row is None
        or (row["rank"] or 0) < 20
        or row["publication"] == "withdrawn"
        or (extraction and (not row["owner"] or not row["generation_allowed"]))
    ):
        raise AccessDenied("forbidden")
    return dict(row)


def submit_processing(conn, principal, source_id, idempotency_key, *, extract=False):
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 200:
        raise ValueError("invalid_idempotency_key")
    with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
        source = _admit(cur, principal, source_id, extraction=extract)
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"processing-submit:{source['library_id']}:{idempotency_key}",),
        )
        cur.execute(
            "SELECT id,source_id,submitted_by,extraction_enabled FROM kb.job "
            "WHERE library_id=%s AND idempotency_key=%s",
            (source["library_id"], idempotency_key),
        )
        existing = cur.fetchone()
        if existing is not None:
            if (
                existing["source_id"],
                existing["submitted_by"],
                existing["extraction_enabled"],
            ) != (source_id, principal.principal_id, extract):
                raise AccessDenied("forbidden")
            return {"job_id": existing["id"], "duplicate_of_existing": True}
        from kb.catalog.admission import check_admission

        check_admission(cur, principal)
        cur.execute(
            """INSERT INTO kb.job(id,library_id,source_id,submitted_by,kind,
          idempotency_key,extraction_enabled) VALUES(%s,%s,%s,%s,'ingest',%s,%s)
          ON CONFLICT(library_id,idempotency_key) DO NOTHING RETURNING id""",
            (
                uuid4(),
                source["library_id"],
                source_id,
                principal.principal_id,
                idempotency_key,
                extract,
            ),
        )
        created = cur.fetchone()
        if not created:
            cur.execute(
                "SELECT id,source_id,submitted_by,extraction_enabled FROM kb.job "
                "WHERE library_id=%s AND idempotency_key=%s",
                (source["library_id"], idempotency_key),
            )
            existing = cur.fetchone()
            if (
                not existing
                or existing["source_id"] != source_id
                or existing["submitted_by"] != principal.principal_id
                or existing["extraction_enabled"] != extract
            ):
                raise AccessDenied("forbidden")
            return {"job_id": existing["id"], "duplicate_of_existing": True}
        _app, task = build_queue("")
        queued = task.configure(connection=conn, lock=f"source:{source_id}").defer(
            job_id=str(created["id"]),
            principal_id=str(principal.principal_id),
            account_id=str(principal.account_id),
        )
        cur.execute("UPDATE kb.job SET queue_id=%s WHERE id=%s", (queued, created["id"]))
        return {"job_id": created["id"], "duplicate_of_existing": False}


def _chunks(fragments):
    batch, size = [], 0
    for row in fragments:
        # Bound escaping overhead as well as text and number of cited fragments.
        for offset in range(0, len(row["text"]), 2000):
            item = {"id": str(row["id"]), "text": row["text"][offset : offset + 2000]}
            length = len(json.dumps(item, ensure_ascii=False)) + 2
            if batch and (size + length > 14000 or len(batch) >= 190):
                yield batch
                batch, size = [], 0
            batch.append(item)
            size += length
    if batch:
        yield batch


def _save_draft(cur, source, draft):
    for item in draft.knowledge:
        ident = uuid4()
        cur.execute(
            "INSERT INTO kb.knowledge(id,library_id,statement,kind) VALUES(%s,%s,%s,%s)",
            (ident, source["library_id"], item.statement, item.kind),
        )
        for fragment_id in dict.fromkeys(item.fragment_ids):
            cur.execute(
                """INSERT INTO kb.knowledge_provenance(knowledge_id,source_id,fragment_id,
              author,publication_ref,url,retrieved_at) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    ident,
                    source["id"],
                    fragment_id,
                    source["author"],
                    source["publication_ref"],
                    source["origin_url"],
                    source["retrieved_at"],
                ),
            )
    for item in draft.rules:
        ident = uuid4()
        cur.execute(
            """INSERT INTO kb.rule(id,library_id,version_no,title,when_to_apply,
          expected_effect,verification,inputs,preconditions,exceptions)
          VALUES(%s,%s,1,%s,%s,%s,%s,%s,%s,%s)""",
            (
                ident,
                source["library_id"],
                item.title,
                item.when_to_apply,
                item.expected_effect,
                item.verification,
                Jsonb(item.inputs),
                Jsonb(item.preconditions),
                Jsonb(item.exceptions),
            ),
        )
        for ordinal, action in enumerate(item.actions):
            cur.execute(
                "INSERT INTO kb.rule_action(rule_id,ordinal,body) VALUES(%s,%s,%s)",
                (ident, ordinal, action),
            )
        for fragment in item.fragment_ids:
            cur.execute(
                "INSERT INTO kb.rule_provenance(rule_id,source_id,fragment_id) VALUES(%s,%s,%s)",
                (ident, source["id"], fragment),
            )


def _set_processing(conn, source_id, state):
    if not conn.execute(
        "UPDATE kb.source SET processing=%s WHERE id=%s RETURNING id", (state, source_id)
    ).fetchone():
        raise AccessDenied("forbidden")


def _claim_job(conn, job_id, principal):
    """Called in a short transaction; no parser or provider runs while holding its lock."""
    job = conn.execute(
        "SELECT *,lease_until>now() AS lease_active FROM kb.job WHERE id=%s FOR UPDATE",
        (job_id,),
    ).fetchone()
    if not job or job["submitted_by"] != principal.principal_id or job["status"] == "done":
        return None
    if job["status"] == "running" and job["lease_active"]:
        raise AttemptInProgress("ingestion_attempt_in_progress")
    source = _admit(conn, principal, job["source_id"], extraction=job["extraction_enabled"])
    claimed = conn.execute(
        "UPDATE kb.job SET status='running',error_code=NULL,started_at=now(),"
        "completed_at=NULL,attempt_token=%s,lease_until=now()+make_interval(secs=>%s) "
        "WHERE id=%s RETURNING *",
        (uuid4(), _LEASE_SECONDS, job_id),
    ).fetchone()
    if not claimed:
        raise AccessDenied("forbidden")
    _set_processing(conn, source["id"], "running")
    return dict(claimed), source


def _fence_attempt(conn, job_id, principal, attempt):
    """Lock and re-admit the exact current attempt before every database mutation."""
    job = conn.execute("SELECT * FROM kb.job WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
    if not job or job["submitted_by"] != principal.principal_id:
        # RLS can hide a revoked job: only the narrow finalizer may then change it.
        raise AccessDenied("forbidden")
    if job["attempt_token"] != attempt or job["status"] != "running":
        raise _StaleAttempt
    source = _admit(conn, principal, job["source_id"], extraction=job["extraction_enabled"])
    if not conn.execute(
        "UPDATE kb.job SET lease_until=now()+make_interval(secs=>%s) "
        "WHERE id=%s AND attempt_token=%s AND status='running' RETURNING id",
        (_LEASE_SECONDS, job_id, attempt),
    ).fetchone():
        raise AccessDenied("forbidden")
    return dict(job), source


def _set_progress(conn, job_id, attempt, completed, total):
    if not conn.execute(
        "UPDATE kb.job SET chunks_done=%s,chunks_total=%s "
        "WHERE id=%s AND attempt_token=%s AND status='running' RETURNING id",
        (completed, total, job_id, attempt),
    ).fetchone():
        raise AccessDenied("forbidden")


def _finish_revoked(conn, job_id, principal, attempt):
    with transaction_identity(conn, principal):
        conn.execute(
            "SELECT kb.finish_revoked_job(%s,%s,%s,%s)",
            (job_id, principal.principal_id, principal.account_id, attempt),
        )


def run_job(dsn, job_id, principal, blob_root, runner_url=None, worker_token=None):
    attempt = None
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        try:
            with transaction_identity(conn, principal):
                claim = _claim_job(conn, job_id, principal)
            if claim is None:
                _finish_revoked(conn, job_id, principal, None)
                return
            # The claim is owned only after the transaction has committed successfully.
            job, source = claim
            attempt = job["attempt_token"]
            store = LocalBlobStore(blob_root)
            _, original_hash = store.stat(source["object_key"])
            if original_hash != source["content_hash"]:
                raise ParseRejected("original_hash_mismatch")
            parsed = parse_document(
                _resolve_in_root(store.root, source["object_key"]), source["media_type"]
            )
            with transaction_identity(conn, principal):
                _, source = _fence_attempt(conn, job_id, principal, attempt)
                for part in parsed:
                    conn.execute(
                        """INSERT INTO kb.fragment(source_id,ordinal,text,locator_kind,
                      file_page,printed_label,chapter,paragraph,snapshot_url,snapshot_at,table_index,cell_row,cell_column)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT(source_id,ordinal) DO NOTHING""",
                        (
                            source["id"],
                            part["ordinal"],
                            part["text"],
                            part["locator_kind"],
                            part.get("file_page"),
                            part.get("printed_label"),
                            part.get("chapter"),
                            part.get("paragraph"),
                            source["origin_url"],
                            source["retrieved_at"],
                            part.get("table_index"),
                            part.get("cell_row"),
                            part.get("cell_column"),
                        ),
                    )
                fragments = conn.execute(
                    "SELECT id,text FROM kb.fragment WHERE source_id=%s ORDER BY ordinal",
                    (source["id"],),
                ).fetchall()
            chunks = list(_chunks(fragments)) if job["extraction_enabled"] else []
            calls, completed = 0, 0
            call_budget = int(os.environ.get("KB_MAX_MODEL_CALLS_PER_JOB", "30"))
            for number, chunk in enumerate(chunks):
                payload = json.dumps(chunk, ensure_ascii=False)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                with transaction_identity(conn, principal):
                    _, source = _fence_attempt(conn, job_id, principal, attempt)
                    found = conn.execute(
                        "SELECT 1 FROM kb.extraction_checkpoint WHERE "
                        "source_id=%s AND chunk_no=%s AND content_hash=%s AND model=%s",
                        (source["id"], number, digest, MODEL),
                    ).fetchone()
                    if found:
                        completed += 1
                    _set_progress(conn, job_id, attempt, completed, len(chunks))
                if found:
                    continue
                if calls >= call_budget:
                    break
                if not runner_url or not worker_token:
                    raise ParseRejected("model_unconfigured")
                calls += 1
                with httpx.Client(timeout=350) as client:
                    response = client.post(
                        runner_url.rstrip("/") + "/generate",
                        headers={"X-KB-Worker-Token": worker_token},
                        json={
                            "task_id": str(job_id),
                            "text": payload,
                            "fragment_ids": [item["id"] for item in chunk],
                        },
                    )
                    if response.status_code != 200:
                        raise ParseRejected("model_unavailable")
                    draft = GenerateResponse.model_validate(response.json())
                permitted = {UUID(item["id"]) for item in chunk}
                if (
                    draft.model != MODEL
                    or draft.task_id != job_id
                    or any(
                        f not in permitted
                        for item in [*draft.knowledge, *draft.rules]
                        for f in item.fragment_ids
                    )
                ):
                    raise ParseRejected("invalid_model_output")
                with transaction_identity(conn, principal), conn.cursor() as cur:
                    _, source = _fence_attempt(conn, job_id, principal, attempt)
                    # This unique admission and all drafts commit atomically. An existing
                    # checkpoint belongs to a completed chunk, never a second set of drafts.
                    inserted = cur.execute(
                        "INSERT INTO kb.extraction_checkpoint(source_id,chunk_no,content_hash,"
                        "model) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING chunk_no",
                        (source["id"], number, digest, MODEL),
                    ).fetchone()
                    if inserted:
                        _save_draft(cur, source, draft)
                    completed += 1
                    _set_progress(conn, job_id, attempt, completed, len(chunks))
            state = "partial" if completed < len(chunks) else "done"
            with transaction_identity(conn, principal):
                _, source = _fence_attempt(conn, job_id, principal, attempt)
                if not conn.execute(
                    "UPDATE kb.job SET status=%s,chunks_done=%s,chunks_total=%s,"
                    "completed_at=now(),error_code=%s,lease_until=NULL "
                    "WHERE id=%s AND attempt_token=%s AND status='running' RETURNING id",
                    (
                        state,
                        completed,
                        len(chunks),
                        "budget_reached" if state == "partial" else None,
                        job_id,
                        attempt,
                    ),
                ).fetchone():
                    raise AccessDenied("forbidden")
                _set_processing(conn, source["id"], state)
        except AttemptInProgress:
            raise
        except _StaleAttempt:
            return
        except AccessDenied:
            _finish_revoked(conn, job_id, principal, attempt)
            return
        except Exception as exc:
            if attempt is None:
                raise RuntimeError("ingestion_not_complete") from None
            code = getattr(exc, "code", "processing_failed")
            state = (
                "needs_ocr"
                if isinstance(exc, NeedsOCR)
                else (
                    "failed"
                    if isinstance(exc, ParseRejected) and code != "model_unavailable"
                    else "partial"
                )
            )
            try:
                with transaction_identity(conn, principal):
                    _, source = _fence_attempt(conn, job_id, principal, attempt)
                    if not conn.execute(
                        "UPDATE kb.job SET status=%s,error_code=%s,completed_at=now(),"
                        "lease_until=NULL WHERE id=%s AND attempt_token=%s "
                        "AND status='running' RETURNING id",
                        (state, code, job_id, attempt),
                    ).fetchone():
                        raise AccessDenied("forbidden")
                    _set_processing(conn, source["id"], state)
            except _StaleAttempt:
                return
            except AccessDenied:
                _finish_revoked(conn, job_id, principal, attempt)
                return
            if isinstance(exc, ParseRejected) and code != "model_unavailable":
                return
            raise RuntimeError("ingestion_not_complete") from None


def main():
    app, _ = build_queue(os.environ["KB_WORKER_DATABASE_URL"])
    with app.open():
        app.run_worker(queues=["ingestion"], concurrency=1)
