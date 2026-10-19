"""Independent real-PostgreSQL replay checks for manual semantic writes."""

from threading import Event
from time import monotonic
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.research import semantic

pytestmark = pytest.mark.integration


@pytest.fixture
def synthetic_admin(world):
    """Use a dedicated admin connection only to observe PostgreSQL lock state."""
    with psycopg.connect(world["admin"], autocommit=True, row_factory=dict_row) as conn:
        yield conn


def _advisory_lock_activity(admin, pid):
    return admin.execute(
        """
        SELECT lock_rows.database, lock_rows.classid, lock_rows.objid,
               lock_rows.objsubid, lock_rows.mode, lock_rows.granted,
               pg_catalog.pg_blocking_pids(lock_rows.pid) AS blocked_by,
               activity.wait_event_type, activity.wait_event,
               activity.state, activity.query
        FROM pg_catalog.pg_locks AS lock_rows
        JOIN pg_catalog.pg_stat_activity AS activity USING (pid)
        WHERE lock_rows.pid=%s AND lock_rows.locktype='advisory'
        """,
        (pid,),
    ).fetchall()


def _wait_for_advisory_wait(admin, holder_pid, waiter_pid):
    assert holder_pid != waiter_pid
    holder_locks = [row for row in _advisory_lock_activity(admin, holder_pid) if row["granted"]]
    assert holder_locks
    deadline = monotonic() + 10
    poll = Event()
    while True:
        waiter_locks = _advisory_lock_activity(admin, waiter_pid)
        waiting = [
            row
            for row in waiter_locks
            if not row["granted"] and holder_pid in (row["blocked_by"] or [])
        ]
        if waiting:
            waiting_row = waiting[0]
            assert waiting_row["wait_event_type"] == "Lock"
            assert waiting_row["wait_event"] == "advisory"
            assert waiting_row["state"] == "active"
            assert "pg_advisory_xact_lock" in (waiting_row["query"] or "")
            key_fields = ("database", "classid", "objid", "objsubid")
            assert any(
                all(waiting_row[field] == holder_row[field] for field in key_fields)
                for holder_row in holder_locks
            )
            return waiting_row
        if monotonic() >= deadline:
            pytest.fail(
                "second transaction never waited on the holder's advisory lock; "
                f"holder={holder_locks!r}; waiter={waiter_locks!r}"
            )
        poll.wait(0.01)


@pytest.mark.parametrize("kind", ["concept", "relation"])
def test_semantic_creation_replay_and_payload_conflict(world, kind):
    key = str(uuid4())

    def create(conn, suffix="", operation_key=key):
        if kind == "concept":
            return semantic.create_concept(
                conn,
                world["principal"],
                world["lib"],
                "Replay" + suffix,
                "Body",
                idempotency_key=operation_key,
            )
        return semantic.create_relation(
            conn,
            world["principal"],
            world["lib"],
            first["id"],
            second["id"],
            "supports" if not suffix else "part_of",
            idempotency_key=operation_key,
        )

    with psycopg.connect(world["dsn"]) as conn:
        first = semantic.create_concept(conn, world["principal"], world["lib"], "First", "Body")
        second = semantic.create_concept(conn, world["principal"], world["lib"], "Second", "Body")
        result = create(conn)
        assert create(conn) == result
        with pytest.raises(ValueError, match="idempotency_conflict"):
            create(conn, "changed")
        assert create(conn) == result
        assert create(conn, operation_key=str(uuid4()))["id"] != result["id"]


@pytest.mark.parametrize("kind", ["concept", "relation"])
def test_semantic_creation_concurrent_replay_and_revoke(world, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from kb.access.policy import AccessDenied

    with psycopg.connect(world["dsn"]) as conn:
        a = semantic.create_concept(conn, world["principal"], world["lib"], "A", "Body")
        b = semantic.create_concept(conn, world["principal"], world["lib"], "B", "Body")
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) "
            "VALUES(%s,%s,'contributor')",
            (world["lib"], world["other"].principal_id),
        )
    # Endpoints belong to the same contributor: another owner's drafts are private.
    with psycopg.connect(world["dsn"]) as conn:
        a = semantic.create_concept(conn, world["other"], world["lib"], "Own A", "Body")
        b = semantic.create_concept(conn, world["other"], world["lib"], "Own B", "Body")
    key = str(uuid4())
    barrier = Barrier(2)

    def invoke(wait=False):
        with psycopg.connect(world["dsn"]) as conn:
            if wait:
                barrier.wait(timeout=10)
            if kind == "concept":
                return semantic.create_concept(
                    conn, world["other"], world["lib"], "Concurrent", "Body", idempotency_key=key
                )
            return semantic.create_relation(
                conn,
                world["other"],
                world["lib"],
                a["id"],
                b["id"],
                "supports",
                idempotency_key=key,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, True) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert results[0] == results[1]
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (world["lib"], world["other"].principal_id),
        )
    with pytest.raises(AccessDenied):
        invoke()


@pytest.mark.parametrize("kind", ["concept", "relation"])
def test_semantic_receipt_failure_rolls_back_entity(world, monkeypatch, kind):
    key = str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        a = semantic.create_concept(conn, world["principal"], world["lib"], "A", "Body")
        b = semantic.create_concept(conn, world["principal"], world["lib"], "B", "Body")
    table = "semantic_concept" if kind == "concept" else "semantic_relation"
    entity_count_query = sql.SQL("SELECT count(*) FROM kb.{} WHERE library_id=%s").format(
        sql.Identifier(table)
    )
    with psycopg.connect(world["admin"]) as conn:
        before = conn.execute(entity_count_query, (world["lib"],)).fetchone()[0]
    original = semantic.record_create_operation

    def fail_record(*args, **kwargs):
        raise RuntimeError("synthetic receipt persistence failure")

    monkeypatch.setattr(semantic, "record_create_operation", fail_record)
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(RuntimeError, match="synthetic receipt"):
            if kind == "concept":
                semantic.create_concept(
                    conn, world["principal"], world["lib"], "Rollback", "Body", idempotency_key=key
                )
            else:
                semantic.create_relation(
                    conn,
                    world["principal"],
                    world["lib"],
                    a["id"],
                    b["id"],
                    "supports",
                    idempotency_key=key,
                )
    monkeypatch.setattr(semantic, "record_create_operation", original)
    with psycopg.connect(world["admin"]) as conn:
        after = conn.execute(entity_count_query, (world["lib"],)).fetchone()[0]
        receipts = conn.execute(
            "SELECT count(*) FROM kb.operation_receipt WHERE idempotency_key=%s", (key,)
        ).fetchone()[0]
    assert after == before
    assert receipts == 0


def test_semantic_withdraw_prechecks_access_before_endpoint_lock(world, monkeypatch):
    from kb.access.policy import AccessDenied

    with psycopg.connect(world["admin"]) as admin:
        admin.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) "
            "VALUES(%s,%s,'contributor')",
            (world["lib"], world["other"].principal_id),
        )
    try:
        with psycopg.connect(world["dsn"]) as conn:
            published = semantic.create_concept(
                conn, world["principal"], world["lib"], "Published", "Body"
            )
            semantic.review_semantic(
                conn, world["principal"], published["id"], "concept", "publish"
            )
            private = semantic.create_concept(conn, world["other"], world["lib"], "Private", "Body")

        lock_calls = []
        original = semantic._lock_semantic_endpoint

        def observed_lock(cur, concept_id):
            lock_calls.append(concept_id)
            return original(cur, concept_id)

        monkeypatch.setattr(semantic, "_lock_semantic_endpoint", observed_lock)
        with psycopg.connect(world["dsn"]) as conn:
            with pytest.raises(AccessDenied, match="forbidden"):
                semantic.review_semantic(
                    conn,
                    world["other"],
                    published["id"],
                    "concept",
                    "withdraw",
                )
        with psycopg.connect(world["dsn"]) as conn:
            with pytest.raises(AccessDenied, match="forbidden"):
                semantic.review_semantic(
                    conn,
                    world["principal"],
                    private["id"],
                    "concept",
                    "withdraw",
                )
        assert lock_calls == []
    finally:
        with psycopg.connect(world["admin"]) as admin:
            admin.execute(
                "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
                (world["lib"], world["other"].principal_id),
            )


@pytest.mark.parametrize("first_operation", ["withdraw", "create"])
def test_relation_create_orders_with_endpoint_withdrawal(
    world, synthetic_admin, monkeypatch, first_operation
):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue

    from kb.access.policy import AccessDenied

    with psycopg.connect(world["admin"]) as admin:
        admin.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) "
            "VALUES(%s,%s,'contributor')",
            (world["lib"], world["other"].principal_id),
        )
    try:
        # Published owner concepts are visible to both the contributor creating
        # the relation and the authorized owner withdrawing an endpoint.
        with psycopg.connect(world["dsn"]) as conn:
            a = semantic.create_concept(conn, world["principal"], world["lib"], "A", "Body")
            b = semantic.create_concept(conn, world["principal"], world["lib"], "B", "Body")
            for concept in (a, b):
                semantic.review_semantic(
                    conn, world["principal"], concept["id"], "concept", "publish"
                )

        locked, release = Event(), Event()
        holder = Queue()
        original = semantic._lock_semantic_endpoint

        def observed_lock(cur, concept_id):
            original(cur, concept_id)
            if concept_id == a["id"] and not locked.is_set():
                holder.put(cur.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
                locked.set()
                assert release.wait(10)

        monkeypatch.setattr(semantic, "_lock_semantic_endpoint", observed_lock)
        creator = world["other"]
        reviewer = world["principal"]

        def invoke(operation, announce=None):
            actor = creator if operation == "create" else reviewer
            with psycopg.connect(world["dsn"]) as conn:
                if announce is not None:
                    announce.put(conn.info.backend_pid)
                if operation == "withdraw":
                    return semantic.review_semantic(conn, actor, a["id"], "concept", "withdraw")
                return semantic.create_relation(
                    conn,
                    actor,
                    world["lib"],
                    a["id"],
                    b["id"],
                    "supports",
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(invoke, first_operation)
            try:
                assert locked.wait(10)
                holder_pid = holder.get(timeout=10)
                second_started = Queue()
                second = pool.submit(
                    invoke,
                    "create" if first_operation == "withdraw" else "withdraw",
                    second_started,
                )
                waiter_pid = second_started.get(timeout=10)
                _wait_for_advisory_wait(synthetic_admin, holder_pid, waiter_pid)
            finally:
                # Do not release the holder until both PostgreSQL catalogs prove
                # that the distinct backend is waiting on its advisory lock.
                release.set()
            first.result(timeout=20)
            if first_operation == "withdraw":
                with pytest.raises(AccessDenied):
                    second.result(timeout=20)
            else:
                assert second.result(timeout=20)["publication"] == "withdrawn"

        with psycopg.connect(world["dsn"]) as conn:
            result = semantic.semantic_network(conn, world["principal"], world["lib"])
            assert not any(node["entity_id"] == a["id"] for node in result["nodes"])
            assert not result["edges"]
    finally:
        with psycopg.connect(world["admin"]) as admin:
            admin.execute(
                "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
                (world["lib"], world["other"].principal_id),
            )


@pytest.mark.parametrize("kind", ["concept", "relation"])
def test_mcp_semantic_create_keyed_replay(world, kind):
    import asyncio

    from fastapi.encoders import jsonable_encoder
    from mcp.server.mcpserver import MCPServer

    from kb.mcp.research import install_tools

    observed = []

    def dispatch(ctx, handler, *args):
        with psycopg.connect(world["dsn"]) as conn:
            result = handler(conn, world["principal"], *args)
            observed.append(result)
            return jsonable_encoder(result)

    server = MCPServer("semantic replay fixture")
    install_tools(server, dispatch)
    key = str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        a = semantic.create_concept(conn, world["principal"], world["lib"], "A", "Body")
        b = semantic.create_concept(conn, world["principal"], world["lib"], "B", "Body")

    async def scenario():
        tool_name = "create_semantic_" + kind
        catalog = {tool.name: tool for tool in await server.list_tools()}
        schema = catalog[tool_name].input_schema
        assert "idempotency_key" in schema["properties"]
        assert "idempotency_key" not in schema.get("required", [])
        arguments = {"library_id": str(world["lib"]), "idempotency_key": key}
        if kind == "concept":
            arguments.update(label="MCP replay", body="Body")
        else:
            arguments.update(
                from_concept=str(a["id"]), to_concept=str(b["id"]), predicate="supports"
            )
        for _ in range(2):
            result = await server.call_tool(tool_name, arguments)
            assert not result.is_error

    asyncio.run(scenario())
    assert len(observed) == 2 and observed[0] == observed[1]
    with psycopg.connect(world["admin"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM kb.operation_receipt WHERE idempotency_key=%s",
                (key,),
            ).fetchone()[0]
            == 1
        )
