"""Real PostgreSQL regressions for retried experience events and current access."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from test_runtime import rule
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied
from kb.catalog import writes

pytestmark = pytest.mark.integration


def test_start_use_retry_keeps_one_use_and_experience(world):
    published, key = rule(world), str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        first = writes.start_use(
            conn, world["principal"], published["id"], 1, "Measure", "Pilot", idempotency_key=key
        )
        repeated = writes.start_use(
            conn, world["principal"], published["id"], 1, "Measure", "Pilot", idempotency_key=key
        )
        assert repeated == first
    with psycopg.connect(world["admin"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM kb.use_record WHERE principal_id=%s AND idempotency_key=%s",
            (world["principal"].principal_id, key),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT count(*) FROM kb.experience WHERE use_id=%s", (first["id"],)
        ).fetchone() == (1,)


@pytest.mark.parametrize("changed", ["rule", "expected_result", "where_applied"])
def test_start_use_key_conflict_never_creates_a_second_use(world, changed):
    published, other_rule, key = rule(world), rule(world), str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        first = writes.start_use(
            conn, world["principal"], published["id"], 1, "Measure", "Pilot", idempotency_key=key
        )
        rule_id = other_rule["id"] if changed == "rule" else published["id"]
        expected = "Changed" if changed == "expected_result" else "Measure"
        location = "Changed" if changed == "where_applied" else "Pilot"
        with pytest.raises(ValueError, match=r"^idempotency_conflict$"):
            writes.start_use(
                conn, world["principal"], rule_id, 1, expected, location, idempotency_key=key
            )
        assert [row["id"] for row in writes.list_uses(conn, world["principal"])] == [first["id"]]


@pytest.mark.parametrize("key", ["", " \t\n", "x" * 201, 7, True])
def test_invalid_keys_are_rejected_before_experience_writes(world, key):
    published = rule(world)
    with psycopg.connect(world["dsn"]) as conn:
        use = writes.start_use(conn, world["principal"], published["id"], 1)
        writes.mark_applied(conn, world["principal"], use["id"])
        with pytest.raises(ValueError, match=r"^invalid_idempotency_key$"):
            writes.start_use(conn, world["principal"], published["id"], 1, idempotency_key=key)
        with pytest.raises(ValueError, match=r"^invalid_idempotency_key$"):
            writes.record_outcome(
                conn, world["principal"], use["id"], "Observed", idempotency_key=key
            )
        rows = writes.list_uses(conn, world["principal"])
        assert len(rows) == 1 and rows[0]["outcomes"] == []


def test_outcome_retry_preserves_event_identity_after_later_corrections(world):
    published, key, correction_key = rule(world), str(uuid4()), str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        use = writes.start_use(conn, world["principal"], published["id"], 1)
        writes.mark_applied(conn, world["principal"], use["id"])
        first = writes.record_outcome(
            conn, world["principal"], use["id"], "Observed", 3, "Pilot", idempotency_key=key
        )
        correction = writes.record_outcome(
            conn,
            world["principal"],
            use["id"],
            "Correction",
            4,
            "Follow-up",
            True,
            idempotency_key=correction_key,
        )
        assert (
            writes.record_outcome(
                conn, world["principal"], use["id"], "Observed", 3, "Pilot", idempotency_key=key
            )
            == first
        )
        assert (
            writes.record_outcome(
                conn,
                world["principal"],
                use["id"],
                "Correction",
                4,
                "Follow-up",
                True,
                idempotency_key=correction_key,
            )
            == correction
        )
        alternatives = [
            {"observed_result": "Different", "rating": 3, "rating_basis": "Pilot"},
            {"observed_result": "Observed", "rating": 5, "rating_basis": "Pilot"},
            {"observed_result": "Observed", "rating": 3, "rating_basis": "Different"},
            {
                "observed_result": "Observed",
                "rating": 3,
                "rating_basis": "Pilot",
                "is_correction": True,
            },
        ]
        for payload in alternatives:
            with pytest.raises(ValueError, match=r"^idempotency_conflict$"):
                writes.record_outcome(
                    conn, world["principal"], use["id"], **payload, idempotency_key=key
                )
        second_use = writes.start_use(conn, world["principal"], published["id"], 1)
        with pytest.raises(ValueError, match=r"^idempotency_conflict$"):
            writes.record_outcome(
                conn,
                world["principal"],
                second_use["id"],
                "Observed",
                3,
                "Pilot",
                idempotency_key=key,
            )
        rows = writes.list_uses(conn, world["principal"])
        original = next(row for row in rows if row["id"] == use["id"])
        assert original["state"] == "corrected"
        assert {event["id"] for event in original["outcomes"]} == {first["id"], correction["id"]}


def test_mark_applied_retries_preserve_later_states(world):
    published = rule(world)
    with psycopg.connect(world["dsn"]) as conn:
        use = writes.start_use(conn, world["principal"], published["id"], 1)
        assert writes.mark_applied(conn, world["principal"], use["id"])["state"] == "applied"
        assert writes.mark_applied(conn, world["principal"], use["id"])["state"] == "applied"
        writes.record_outcome(conn, world["principal"], use["id"], "Observed")
        assert writes.mark_applied(conn, world["principal"], use["id"])["state"] == "outcomed"
        writes.record_outcome(conn, world["principal"], use["id"], "Corrected", is_correction=True)
        assert writes.mark_applied(conn, world["principal"], use["id"])["state"] == "corrected"


def test_revocation_closes_keyed_retries_and_confirmation_without_deleting_history(world):
    published, use_key, outcome_key = rule(world), str(uuid4()), str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        use = writes.start_use(
            conn, world["principal"], published["id"], 1, idempotency_key=use_key
        )
        writes.mark_applied(conn, world["principal"], use["id"])
        outcome = writes.record_outcome(
            conn, world["principal"], use["id"], "Observed", idempotency_key=outcome_key
        )
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (world["lib"], world["principal"].principal_id),
        )
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(AccessDenied):
            writes.start_use(conn, world["principal"], published["id"], 1, idempotency_key=use_key)
        with pytest.raises(AccessDenied):
            writes.mark_applied(conn, world["principal"], use["id"])
        with pytest.raises(AccessDenied):
            writes.record_outcome(
                conn, world["principal"], use["id"], "Observed", idempotency_key=outcome_key
            )
    with psycopg.connect(world["admin"]) as conn:
        assert conn.execute(
            "SELECT state FROM kb.use_record WHERE id=%s", (use["id"],)
        ).fetchone() == ("outcomed",)
        assert conn.execute(
            "SELECT id FROM kb.outcome WHERE use_id=%s", (use["id"],)
        ).fetchone() == (outcome["id"],)


def test_foreign_author_cannot_replay_or_change_anothers_use(world):
    published, key = rule(world), str(uuid4())
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,'reader')",
            (world["lib"], world["other"].principal_id),
        )
    with psycopg.connect(world["dsn"]) as conn:
        use = writes.start_use(conn, world["principal"], published["id"], 1, idempotency_key=key)
        writes.mark_applied(conn, world["principal"], use["id"])
        event = writes.record_outcome(
            conn, world["principal"], use["id"], "Private", idempotency_key=key
        )
        other_use = writes.start_use(conn, world["other"], published["id"], 1, idempotency_key=key)
        assert other_use["id"] != use["id"]
        writes.mark_applied(conn, world["other"], other_use["id"])
        other_event = writes.record_outcome(
            conn, world["other"], other_use["id"], "Other report", idempotency_key=key
        )
        assert other_event["id"] != event["id"]
        assert other_event["reported_by"] == world["other"].principal_id
        with pytest.raises(AccessDenied):
            writes.mark_applied(conn, world["other"], use["id"])
        with pytest.raises(AccessDenied):
            writes.record_outcome(conn, world["other"], use["id"], "Changed", idempotency_key=key)
        assert writes.list_uses(conn, world["principal"])[0]["outcomes"] == [event]


@pytest.mark.parametrize("operation", ["use", "outcome"])
def test_concurrent_duplicate_requests_create_one_event(world, operation):
    published, key = rule(world), str(uuid4())
    use_id = None
    if operation == "outcome":
        with psycopg.connect(world["dsn"]) as conn:
            use = writes.start_use(conn, world["principal"], published["id"], 1)
            writes.mark_applied(conn, world["principal"], use["id"])
            use_id = use["id"]
    barrier = Barrier(2, timeout=10)

    def request():
        with psycopg.connect(world["dsn"]) as conn:
            barrier.wait()
            if operation == "use":
                return writes.start_use(
                    conn, world["principal"], published["id"], 1, "Expected", idempotency_key=key
                )["id"]
            return writes.record_outcome(
                conn, world["principal"], use_id, "Observed", idempotency_key=key
            )["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        ids = [future.result(timeout=20) for future in futures]
    assert ids[0] == ids[1]
    with psycopg.connect(world["admin"]) as conn:
        if operation == "use":
            assert conn.execute(
                "SELECT count(*) FROM kb.use_record WHERE principal_id=%s AND idempotency_key=%s",
                (world["principal"].principal_id, key),
            ).fetchone() == (1,)
            assert conn.execute(
                "SELECT count(*) FROM kb.experience WHERE use_id=%s", (ids[0],)
            ).fetchone() == (1,)
        else:
            assert conn.execute(
                "SELECT count(*) FROM kb.outcome WHERE reported_by=%s AND idempotency_key=%s",
                (world["principal"].principal_id, key),
            ).fetchone() == (1,)
