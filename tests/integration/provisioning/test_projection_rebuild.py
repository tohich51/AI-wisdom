"""Forced semantic projection recovery and explicit rebuild queue regressions."""

from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_incremental_copy import begin_library, checkpoint_rows, run_index
from test_incremental_copy import sdk_writer as sdk_writer
from test_semantic_projection import current
from test_semantic_projection import semantic_library as semantic_library

from kb.access.policy import transaction_identity
from kb.jobs import indexer
from kb.jobs.schema import install_queue

pytestmark = pytest.mark.integration


def test_forced_rebuild_recovers_lost_sdk_files_and_resumes_same_generation(
    semantic_library, sdk_writer, provisioner, gateway, world, monkeypatch
):
    writer, keys_root = sdk_writer
    for ordinal in range(16):
        world.knowledge(semantic_library, title=f"Forced rebuild boundary document {ordinal:02d}")

    initial_result = run_index(provisioner, world.manager, semantic_library, keys_root)
    assert initial_result == {
        "state": "current",
        "documents": 17,
        "unchanged": False,
    }
    old = current(provisioner, world, semantic_library)
    documents = old.snapshot.documents
    canonical = {
        document.id: (document.statement, document.statement_hash) for document in documents
    }
    assert len(documents) == 17
    assert writer.files[old.resource_root] == {
        document.id: document.statement for document in documents
    }
    old_checkpoints = checkpoint_rows(provisioner, world.manager, old)
    assert old_checkpoints == {
        (document.id, old.document_uri(document), document.statement_hash) for document in documents
    }

    # Lose only the synthetic backend files; the verified SQL generation remains intact.
    writer.files.clear()
    writer.copies.clear()
    writer.writes.clear()
    assert run_index(provisioner, world.manager, semantic_library, keys_root) == {
        "state": "current",
        "documents": 17,
        "unchanged": True,
    }
    assert current(provisioner, world, semantic_library).generation == old.generation
    assert writer.files == {}
    assert writer.copies == []
    assert writer.writes == []
    assert checkpoint_rows(provisioner, world.manager, old) == old_checkpoints
    assert gateway.execute(
        "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ).fetchone() == (False, False)

    write_attempts = 0
    original_write_batch = writer.write_batch

    def fail_before_second_batch(root, document_pairs):
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 2:
            raise RuntimeError("synthetic failure before second batch")
        return original_write_batch(root, document_pairs)

    monkeypatch.setattr(writer, "write_batch", fail_before_second_batch)

    def force_rebuild(*, terminal_failure):
        return indexer.reindex(
            provisioner.info.dsn,
            world.manager,
            semantic_library,
            url="opaque://unused",
            keys_root=keys_root,
            terminal_failure=terminal_failure,
            rebuild=True,
        )

    with pytest.raises(RuntimeError, match=r"^semantic_index_not_complete$") as failure:
        force_rebuild(terminal_failure=False)
    cause = failure.value.__context__
    assert write_attempts == 2, (
        type(cause).__name__,
        getattr(getattr(cause, "diag", None), "constraint_name", None),
    )

    with psycopg.connect(provisioner.info.dsn, autocommit=True) as lock_connection:
        assert lock_connection.execute("SELECT pg_try_advisory_lock(112187,1)").fetchone() == (
            True,
        )
        assert lock_connection.execute("SELECT pg_advisory_unlock(112187,1)").fetchone() == (True,)

    pending = begin_library(provisioner, world.manager, semantic_library)
    assert pending.generation > old.generation
    assert not pending.already_current
    assert pending.snapshot.content_hash == old.snapshot.content_hash
    assert pending.snapshot.documents == documents
    first_documents = pending.snapshot.documents[:16]
    remaining_documents = pending.snapshot.documents[16:]

    assert writer.copies == []
    assert len(writer.writes) == 1
    assert writer.writes[0] == (
        pending.resource_root,
        tuple((document.id, document.statement) for document in first_documents),
    )
    assert writer.files[pending.resource_root] == {
        document.id: document.statement for document in first_documents
    }
    assert checkpoint_rows(provisioner, world.manager, pending) == {
        (document.id, pending.document_uri(document), document.statement_hash)
        for document in first_documents
    }
    assert checkpoint_rows(provisioner, world.manager, old) == old_checkpoints

    with transaction_identity(provisioner, world.manager):
        states = dict(
            provisioner.execute(
                "SELECT generation,state FROM kb.index_generation WHERE library_id=%s",
                (semantic_library,),
            ).fetchall()
        )
    assert states == {
        old.generation: "current",
        pending.generation: "building",
    }

    retry_offset = len(writer.writes)
    assert force_rebuild(terminal_failure=False) == {
        "state": "current",
        "documents": 17,
        "unchanged": False,
    }
    assert write_attempts == 3
    assert writer.copies == []
    assert writer.writes[retry_offset:] == [
        (
            pending.resource_root,
            tuple((document.id, document.statement) for document in remaining_documents),
        )
    ]

    active = current(provisioner, world, semantic_library)
    assert active.generation == pending.generation
    assert active.already_current
    assert active.snapshot.documents == documents
    assert writer.files == {
        active.resource_root: {
            document.id: document.statement for document in active.snapshot.documents
        }
    }
    assert [len(pairs) for _root, pairs in writer.writes] == [16, 1]
    written_pairs = [pair for _root, pairs in writer.writes for pair in pairs]
    assert len(written_pairs) == 17
    assert {pair[0] for pair in written_pairs} == set(canonical)
    assert {pair[0]: pair[1] for pair in written_pairs} == {
        document_id: statement for document_id, (statement, _statement_hash) in canonical.items()
    }
    assert checkpoint_rows(provisioner, world.manager, active) == {
        (document.id, active.document_uri(document), document.statement_hash)
        for document in active.snapshot.documents
    }
    assert checkpoint_rows(provisioner, world.manager, old) == old_checkpoints

    with transaction_identity(provisioner, world.manager):
        states = dict(
            provisioner.execute(
                "SELECT generation,state FROM kb.index_generation WHERE library_id=%s",
                (semantic_library,),
            ).fetchall()
        )
    assert states == {
        old.generation: "retired",
        pending.generation: "current",
    }


def test_explicit_rebuild_queue_is_durable_deduplicated_and_resolves_context_fresh(
    semantic_library, world, admin, monkeypatch
):
    install_queue(admin.info.dsn)
    login = "projection_rebuild_queue_" + uuid4().hex[:12]
    admin.execute(
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE kb_app,kb_provisioner"
        ).format(sql.Identifier(login))
    )
    dsn = make_conninfo(admin.info.dsn, user=login)
    app, enqueue = indexer.build_queue(dsn, {})

    with app.open():
        assert enqueue(rebuild_library=semantic_library) is not None
        with pytest.raises(RuntimeError, match=r"^semantic_rebuild_busy_not_queued$"):
            enqueue(rebuild_library=semantic_library)

        rows = admin.execute(
            "SELECT task_name,queue_name,status,args,lock,queueing_lock "
            "FROM procrastinate_jobs WHERE args->>'library_id'=%s",
            (str(semantic_library),),
        ).fetchall()
        assert len(rows) == 1
        task_name, queue_name, status, args, lock, queueing_lock = rows[0]
        assert (task_name, queue_name, status) == (
            "kb.reindex_library",
            "semantic",
            "todo",
        )
        assert args == {
            "library_id": str(semantic_library),
            "rebuild": True,
        }
        assert lock == "semantic-installation"
        assert queueing_lock == "semantic:" + str(semantic_library)

        reindex_calls = []

        def forbidden_reindex(*call_args, **call_kwargs):
            reindex_calls.append((call_args, call_kwargs))
            raise AssertionError("reindex must not run without a fresh semantic context")

        monkeypatch.setattr(indexer, "reindex", forbidden_reindex)
        deactivated = admin.execute(
            "UPDATE kb.membership SET status='deactivated',deactivated_at=now(),"
            "deactivated_by=principal_id "
            "WHERE organisation_id=%s AND principal_id IN (%s,%s) "
            "RETURNING principal_id",
            (world.account_id, world.owner, world.colleague),
        ).fetchall()
        assert {row[0] for row in deactivated} == {world.owner, world.colleague}
        assert admin.execute(
            "SELECT count(*) FROM kb.membership WHERE organisation_id=%s AND status='active'",
            (world.account_id,),
        ).fetchone() == (0,)

        task_context = SimpleNamespace(job=SimpleNamespace(attempts=1))
        assert app.tasks[task_name].func(task_context, **args) == {
            "state": "skipped",
            "reason": "semantic_context_unavailable",
        }
        assert reindex_calls == []

        unknown = uuid4()
        with pytest.raises(RuntimeError, match=r"^semantic_context_unavailable$"):
            enqueue(rebuild_library=unknown)

    assert admin.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE args->>'library_id'=%s",
        (str(unknown),),
    ).fetchone() == (0,)
    assert admin.execute(
        "SELECT count(*) FROM procrastinate_jobs WHERE args->>'library_id'=%s",
        (str(semantic_library),),
    ).fetchone() == (1,)
    assert admin.execute(
        "SELECT rolcanlogin,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole "
        "FROM pg_roles WHERE rolname=%s",
        (login,),
    ).fetchone() == (True, False, False, False, False)
    granted_roles = {
        row[0]
        for row in admin.execute(
            "SELECT granted.rolname FROM pg_auth_members AS membership "
            "JOIN pg_roles AS granted ON granted.oid=membership.roleid "
            "JOIN pg_roles AS member ON member.oid=membership.member "
            "WHERE member.rolname=%s",
            (login,),
        ).fetchall()
    }
    assert granted_roles == {"kb_app", "kb_provisioner"}
