"""Real PostgreSQL boundaries for immutable citations and complete dependency ACL."""

import hashlib
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_runtime import runtime_db as runtime_db
from test_runtime import source
from test_runtime import world as world

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import libraries
from kb.catalog.grounding import capture_snapshot, resolve_citation, validate_citation
from kb.ingestion import run_job, submit_processing

pytestmark = pytest.mark.integration


def parsed(world, text):
    ident = source(world, text)
    with psycopg.connect(world["dsn"]) as conn:
        job = submit_processing(conn, world["principal"], ident, str(uuid4()))
    run_job(world["worker"], job["job_id"], world["principal"], world["store"].root)
    return ident


def complete_run(conn, world, snapshot, sources):
    principal, ident = world["principal"], uuid4()
    with transaction_identity(conn, principal):
        conn.execute(
            """INSERT INTO kb.research_run(id,account_id,principal_id,library_id,source_ids,
              manifest,kind,idempotency_key,request_hash)
              VALUES(%s,%s,%s,%s,%s,%s,'research',%s,%s)""",
            (
                ident,
                principal.account_id,
                principal.principal_id,
                world["lib"],
                sources,
                Jsonb(snapshot["manifest"]),
                str(uuid4()),
                "a" * 64,
            ),
        )
        conn.execute(
            "UPDATE kb.research_run SET status='running',attempt_token=%s WHERE id=%s",
            (uuid4(), ident),
        )
        conn.execute(
            "UPDATE kb.research_run SET status='done',result=%s WHERE id=%s",
            (Jsonb({"items": [], "citations": []}), ident),
        )
    return ident


def test_exact_unicode_citation_and_forged_snapshot(world):
    text = "Маркетинг 🎯: выберите сегмент. Измерьте результат эксперимента."
    ident = parsed(world, text)
    with psycopg.connect(world["dsn"]) as conn:
        snapshot = capture_snapshot(conn, world["principal"], [ident], "результат")
        fragment = snapshot["fragments"][0]
        quote = "Измерьте результат эксперимента."
        with (
            transaction_identity(conn, world["principal"]),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            citation = resolve_citation(cur, snapshot["manifest"], UUID(fragment["id"]), quote)
            assert citation["start"] == len("Маркетинг 🎯: выберите сегмент. ")
            assert citation["quote"] == text[citation["start"] : citation["end"]]
            assert citation["text_hash"] == hashlib.sha256(text.encode()).hexdigest()
            assert citation["locator"]["file_page"] is None
            assert validate_citation(cur, snapshot["manifest"], citation)["id"] == citation["id"]
            for invalid in [
                {**citation, "end": citation["end"] + 1},
                {**citation, "content_hash": "b" * 64},
                {**citation, "source_version_id": str(uuid4())},
            ]:
                with pytest.raises(ValueError):
                    validate_citation(cur, snapshot["manifest"], invalid)
            with pytest.raises(ValueError):
                resolve_citation(
                    cur,
                    snapshot["manifest"],
                    UUID(fragment["id"]),
                    "A claim absent from the source",
                )
        with pytest.raises(AccessDenied):
            capture_snapshot(conn, world["other"], [ident])


def test_revoked_dependency_hides_whole_result_note_and_comment(world):
    first = parsed(world, "Первый источник: измерьте результат.")
    with psycopg.connect(world["dsn"]) as conn:
        second_lib = libraries.create_library(
            conn,
            world["principal"],
            libraries.CreateLibrary(
                organisation_id=world["principal"].account_id, name=str(uuid4()), kind="reference"
            ),
        )
    second = parsed({**world, "lib": second_lib.id}, "Второй источник: назовите исходные данные.")
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            conn.execute(
                "INSERT INTO kb.generation_policy(library_id,generation_allowed) VALUES(%s,true)",
                (second_lib.id,),
            )
        snapshot = capture_snapshot(conn, world["principal"], [first, second])
        run = complete_run(conn, world, snapshot, [first, second])
        with transaction_identity(conn, world["principal"]):
            conn.execute(
                "INSERT INTO kb.research_note(account_id,principal_id,research_run_id,"
                "title,comment)"
                " VALUES(%s,%s,%s,'Private note','Private comment')",
                (world["principal"].account_id, world["principal"].principal_id, run),
            )
            assert conn.execute(
                "SELECT count(*) FROM kb.research_run WHERE id=%s", (run,)
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM kb.research_note WHERE research_run_id=%s", (run,)
            ).fetchone() == (1,)
    with psycopg.connect(world["admin"]) as admin:
        for library in [world["lib"], second_lib.id]:
            admin.execute(
                "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,'reader')",
                (library, world["other"].principal_id),
            )
    with psycopg.connect(world["dsn"]) as conn, transaction_identity(conn, world["other"]):
        assert conn.execute(
            "SELECT kb.can_read_manifest(%s)", (Jsonb(snapshot["manifest"]),)
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT count(*) FROM kb.research_run WHERE id=%s", (run,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM kb.research_note WHERE research_run_id=%s", (run,)
        ).fetchone() == (0,)
    with psycopg.connect(world["admin"]) as admin:
        admin.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (second_lib.id, world["principal"].principal_id),
        )
    with psycopg.connect(world["dsn"]) as conn, transaction_identity(conn, world["principal"]):
        assert conn.execute(
            "SELECT kb.can_read_manifest(%s)", (Jsonb(snapshot["manifest"]),)
        ).fetchone() == (False,)
        assert conn.execute(
            "SELECT count(*) FROM kb.research_run WHERE id=%s", (run,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM kb.research_note WHERE research_run_id=%s", (run,)
        ).fetchone() == (0,)


def test_manifest_frozen_terminal_result_and_default_deny(world):
    ident = parsed(world, "Test a measurable result.")
    with psycopg.connect(world["dsn"]) as conn:
        snapshot = capture_snapshot(conn, world["principal"], [ident])
        run = complete_run(conn, world, snapshot, [ident])
        with pytest.raises(psycopg.Error), transaction_identity(conn, world["principal"]):
            conn.execute(
                "UPDATE kb.research_run SET result=%s WHERE id=%s", (Jsonb({"changed": True}), run)
            )
        with transaction_identity(conn, world["principal"]):
            for invalid in [
                {},
                {"sources": [], "fragments": []},
                {"sources": [{"source_id": "bad"}], "fragments": [None]},
            ]:
                assert conn.execute(
                    "SELECT kb.can_read_manifest(%s)", (Jsonb(invalid),)
                ).fetchone() == (False,)
        assert conn.execute("SELECT count(*) FROM kb.research_run").fetchone() == (0,)


def test_missing_fragment_finalizes_only_worker_owned_pending_run(world):
    ident = parsed(world, "One immutable fragment for a pending run.")
    actor, run = world["principal"], uuid4()
    with psycopg.connect(world["dsn"]) as conn:
        snapshot = capture_snapshot(conn, actor, [ident])
        with transaction_identity(conn, actor):
            conn.execute(
                "INSERT INTO kb.research_run(id,account_id,principal_id,library_id,source_ids,"
                "manifest,kind,idempotency_key,request_hash) "
                "VALUES(%s,%s,%s,%s,%s,%s,'research',%s,%s)",
                (
                    run,
                    actor.account_id,
                    actor.principal_id,
                    world["lib"],
                    [ident],
                    Jsonb(snapshot["manifest"]),
                    str(uuid4()),
                    "c" * 64,
                ),
            )
            conn.execute("UPDATE kb.research_run SET status='running' WHERE id=%s", (run,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "SELECT kb.finish_revoked_research(%s,%s,%s)",
                (run, actor.principal_id, actor.account_id),
            )
    with psycopg.connect(world["admin"]) as admin:
        admin.execute("DELETE FROM kb.fragment WHERE source_id=%s", (ident,))
    with psycopg.connect(world["worker"]) as worker:
        worker.execute(
            "SELECT kb.finish_revoked_research(%s,%s,%s)",
            (run, actor.principal_id, actor.account_id),
        )
    with psycopg.connect(world["admin"]) as admin:
        row = admin.execute(
            "SELECT status,error_code FROM kb.research_run WHERE id=%s", (run,)
        ).fetchone()
        assert row == ("failed", "access_revoked")


def test_partial_excerpt_is_labelled_context_limited(world):
    text = "Начало текста. " + "Дополнение. " * 55 + "Поздниймаркер завершает абзац."
    ident = parsed(world, text)
    with psycopg.connect(world["dsn"]) as conn:
        snapshot = capture_snapshot(conn, world["principal"], [ident], "Поздниймаркер")
        assert snapshot["context_limited"] is True
        assert snapshot["fragments"][0]["start_offset"] > 0
