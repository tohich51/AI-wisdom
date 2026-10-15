"""Combined queue admission is authoritative across actors, libraries and task kinds.

These real-PostgreSQL scenarios enqueue only synthetic data. No worker/provider
is started for queued requests. Root owns execution with migration 0018 and the
ingestion integration present.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from test_research_grounding import parsed
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import Principal, transaction_identity
from kb.catalog import libraries, research
from kb.jobs.ingestion import submit_processing

pytestmark = pytest.mark.integration


def _research(conn, item, source_id, key=None):
    return research.submit_research(
        conn,
        item["principal"],
        item["lib"],
        [source_id],
        "research",
        "What should the synthetic experiment measure?",
        key or str(uuid4()),
    )


def _active(item):
    with psycopg.connect(item["admin"]) as conn:
        return conn.execute(
            "SELECT (SELECT count(*) FROM kb.job j JOIN kb.library l ON l.id=j.library_id "
            "WHERE l.organisation_id=%s AND j.status IN ('queued','running')) + "
            "(SELECT count(*) FROM kb.research_run WHERE account_id=%s "
            "AND status IN ('queued','running'))",
            (item["principal"].account_id, item["principal"].account_id),
        ).fetchone()[0]


def _is_queue_full(error):
    # The shared helper translates only the exact authoritative SQL refusal;
    # unrelated PostgreSQL errors must never masquerade as capacity refusals.
    return isinstance(error, ValueError) and str(error) == "processing_queue_full"


def _private_actor(base):
    principal = Principal(uuid4(), base["principal"].account_id)
    with psycopg.connect(base["admin"]) as conn:
        conn.execute(
            "INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,status) "
            "VALUES(%s,%s,'https://identity.test',%s,'active')",
            (principal.account_id, principal.principal_id, str(principal.principal_id)),
        )
        conn.execute(
            "INSERT INTO kb.organisation_admin(organisation_id,principal_id) VALUES(%s,%s)",
            (principal.account_id, principal.principal_id),
        )
    with psycopg.connect(base["dsn"]) as conn:
        library = libraries.create_library(
            conn,
            principal,
            libraries.CreateLibrary(
                organisation_id=principal.account_id,
                name="Synthetic private admission library " + str(uuid4()),
                kind="reference",
            ),
        )
        with transaction_identity(conn, principal):
            conn.execute(
                "INSERT INTO kb.generation_policy(library_id,generation_allowed) VALUES(%s,true)",
                (library.id,),
            )
    return {**base, "principal": principal, "lib": library.id}


def test_research_counts_ingestion_and_keeps_duplicate_replay_at_actor_limit(world):
    source_id = parsed(world, "Synthetic marketing fixture: measure one qualified lead.")
    key = str(uuid4())
    with psycopg.connect(world["dsn"]) as conn:
        first = _research(conn, world, source_id, key)
        for _ in range(2):
            submit_processing(conn, world["principal"], source_id, str(uuid4()), extract=False)
        assert _active(world) == 3
        duplicate = _research(conn, world, source_id, key)
        assert duplicate["run_id"] == first["run_id"]
        assert duplicate["duplicate_of_existing"] is True
        with pytest.raises(ValueError, match=r"^processing_queue_full$") as refused:
            _research(conn, world, source_id)
        assert _is_queue_full(refused.value)
    assert _active(world) == 3


def test_organisation_limit_counts_other_actors_rls_hidden_requests(world):
    actors = [world, *[_private_actor(world) for _ in range(3)]]
    sources = [
        parsed(actor, "Synthetic private fixture: record a measured result.") for actor in actors
    ]
    for actor, source_id, amount in zip(actors, sources, (3, 3, 3, 1), strict=True):
        with psycopg.connect(actor["dsn"]) as conn:
            for _ in range(amount):
                _research(conn, actor, source_id)
    assert _active(world) == 10
    last = actors[-1]
    with psycopg.connect(last["dsn"]) as conn:
        with transaction_identity(conn, last["principal"]):
            assert conn.execute("SELECT count(*) FROM kb.research_run").fetchone()[0] == 1
        with pytest.raises(ValueError, match=r"^processing_queue_full$") as refused:
            _research(conn, last, sources[-1])
        assert _is_queue_full(refused.value)
    assert _active(world) == 10


def test_concurrent_ingestion_and_research_share_the_last_actor_slot(world):
    source_id = parsed(
        world, "Synthetic concurrent fixture: compare an expected and observed result."
    )
    with psycopg.connect(world["dsn"]) as conn:
        for _ in range(2):
            submit_processing(conn, world["principal"], source_id, str(uuid4()), extract=False)
    assert _active(world) == 2
    barrier = Barrier(2)

    def enqueue(kind):
        with psycopg.connect(world["dsn"]) as conn:
            barrier.wait(timeout=10)
            try:
                if kind == "research":
                    _research(conn, world, source_id)
                else:
                    submit_processing(
                        conn, world["principal"], source_id, str(uuid4()), extract=False
                    )
            except ValueError as error:
                if _is_queue_full(error):
                    return "queue_full"
                raise
            return "queued"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(enqueue, kind) for kind in ("research", "ingestion")]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == ["queue_full", "queued"]
    assert _active(world) == 3
