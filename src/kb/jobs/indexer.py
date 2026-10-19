"""Single-writer, resumable semantic projection on the existing durable queue.

Each queued attempt resolves the library's indexing context immediately before
work; no configured, cached, or process-wide principal is used.
"""

import os
import time
from pathlib import Path
from uuid import UUID

import procrastinate
import psycopg
from procrastinate.exceptions import AlreadyEnqueued

from kb.access.policy import transaction_identity
from kb.adapters.secrets import _reject_symlinks
from kb.adapters.vector_writer import OpenVikingWriter
from kb.retrieval import semantic_registry as registry
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.provisioning import load_account, request_provisioning


def _checkpoint(conn, principal, build, document):
    # FK deletion and revision locks can deadlock; retry SQL only, not embedding.
    for attempt in range(3):
        try:
            registry.checkpoint(conn, principal, build, document)
            return
        except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
            if attempt == 2:
                raise RuntimeError("index_checkpoint_retry_exhausted") from None
            time.sleep(0.1 * (attempt + 1))


def reindex(
    dsn,
    principal,
    library_id,
    *,
    url,
    keys_root,
    terminal_failure=True,
):
    build = None
    writer = None
    with psycopg.connect(dsn, autocommit=True) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(112187,1)").fetchone()[0]:
            raise RuntimeError("index_writer_busy")
        try:
            registry.capture(conn, principal, library_id)
            account = load_account(conn, principal, library_id)
            if account and (
                account.dimension != SELECTED_PROFILE.dimension
                or account.embedding_profile != SELECTED_PROFILE.name
            ):
                raise RuntimeError("projection_profile_mismatch")
            if account is None or account.state != "ready":
                request_provisioning(conn, principal, library_id)
                return {"state": "awaiting_provisioning"}
            with transaction_identity(conn, principal):
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,91))", (str(library_id),)
                )
                conn.execute(
                    "UPDATE kb.index_account SET profile_hash=%s WHERE library_id=%s "
                    "AND state='ready' AND (profile_hash IS NULL OR profile_hash=%s)",
                    (SELECTED_PROFILE.profile_hash, library_id, SELECTED_PROFILE.profile_hash),
                )
            snapshot = registry.capture(conn, principal, library_id)
            build = registry.begin(conn, principal, snapshot)
            if build.already_current:
                return {"state": "current", "documents": len(snapshot.documents), "unchanged": True}
            path = keys_root / "index" / account.index_identity / "key"
            _reject_symlinks(path)
            if path.stat().st_mode & 0o077:
                raise RuntimeError("index_key_permissions")
            writer = OpenVikingWriter(url, path.read_text().strip())
            writer.prepare(build.resource_root)
            completed = registry.completed_documents(conn, principal, build)
            pending = [d for d in snapshot.documents if completed.get(d.id) != d.statement_hash]

            def readmit():
                if (
                    registry.capture(conn, principal, library_id).content_hash
                    != snapshot.content_hash
                ):
                    raise RuntimeError("projection_revision_changed")

            for offset in range(0, len(pending), 16):
                readmit()
                batch = pending[offset : offset + 16]
                writer.write_batch(build.resource_root, [(d.id, d.statement) for d in batch])
                for document in batch:
                    _checkpoint(conn, principal, build, document)
            readmit()
            writer.verify(build.resource_root, [(d.id, d.statement) for d in snapshot.documents])
            if snapshot.documents:
                first = snapshot.documents[0]
                writer.canary(
                    build.resource_root,
                    first.statement[:1200],
                    {build.document_uri(d) for d in snapshot.documents},
                )
            registry.activate(conn, principal, build)
            return {"state": "current", "documents": len(snapshot.documents), "unchanged": False}
        except Exception:
            # Keep checkpoints during bounded retries; free the installation's
            # build slot on terminal failure so other libraries can progress.
            if terminal_failure and build is not None:
                registry.fail(conn, principal, build)
            raise RuntimeError("semantic_index_not_complete") from None
        finally:
            if writer is not None:
                writer.close()
            conn.execute("SELECT pg_advisory_unlock(112187,1)")


def configuration():
    return dict(
        url=os.environ["KB_OPENVIKING_URL"],
        keys_root=Path(os.environ["KB_INDEX_KEYS_ROOT"]),
    )


def build_queue(dsn, config):
    app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn))

    @app.task(
        name="kb.reindex_library",
        queue="semantic",
        pass_context=True,
        retry=procrastinate.RetryStrategy(max_attempts=3, wait=20),
    )
    def index(context, library_id: str):
        library_uuid = UUID(library_id)
        with psycopg.connect(dsn, autocommit=True) as conn:
            principal = registry.resolve_context(conn, library_uuid)
        if principal is None:
            return {"state": "skipped", "reason": "semantic_context_unavailable"}
        return reindex(
            dsn, principal, library_uuid, terminal_failure=context.job.attempts >= 2, **config
        )

    def enqueue(*, rotation: int = 0):
        with psycopg.connect(dsn, autocommit=True) as conn:
            registry.retire_unavailable_builds(conn)
            for library_id in registry.pending_libraries(conn, rotation=rotation):
                try:
                    index.configure(
                        lock="semantic-installation",
                        queueing_lock=f"semantic:{library_id}",
                    ).defer(library_id=str(library_id))
                except AlreadyEnqueued:
                    pass

    @app.periodic(cron="* * * * *", queue="semantic")
    @app.task(name="kb.poll_semantic", queue="semantic")
    def poll(timestamp: int):
        enqueue(rotation=timestamp // 60)

    return app, enqueue


def main():
    dsn = os.environ["KB_INDEXER_DATABASE_URL"]
    app, enqueue = build_queue(dsn, configuration())
    with app.open():
        enqueue(rotation=int(time.time() // 60))
        app.run_worker(queues=["semantic"], concurrency=1)
