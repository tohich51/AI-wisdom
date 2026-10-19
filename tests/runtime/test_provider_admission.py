"""Independent C14 acceptance: real PostgreSQL, processes and synthetic jobs."""

import hashlib
import multiprocessing as mp
import os
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from test_runtime import runtime_db as runtime_db
from test_runtime import source
from test_runtime import world as world

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.jobs.ingestion import _claim_job, submit_processing
from kb.jobs.provider_admission import ProviderBusy, ProviderReviewRequired, call_provider

pytestmark = pytest.mark.integration


def _key():
    return hashlib.sha256(uuid4().bytes).hexdigest()


def _job(item):
    with psycopg.connect(item["dsn"]) as conn:
        job = submit_processing(conn, item["principal"], source(item), str(uuid4()), extract=True)[
            "job_id"
        ]
    attempt = uuid4()
    with psycopg.connect(item["admin"]) as conn:
        conn.execute(
            "UPDATE kb.job SET status='running',started_at=now(),attempt_token=%s,"
            "lease_until=now()+interval '8 minutes' WHERE id=%s",
            (attempt, job),
        )
    return job, attempt


def _send_in_process(dsn, identity, job, attempt, key, barrier, release, messages):
    principal = Principal(*identity)
    with psycopg.connect(dsn) as conn:
        if barrier is not None:
            barrier.wait(timeout=20)

        def send():
            messages.put(("sending", os.getpid(), conn.info.backend_pid))
            if not release.wait(timeout=20):
                raise TimeoutError("synthetic provider release missing")
            return httpx.Response(200, json={"synthetic": True})

        try:
            call_provider(
                conn,
                principal,
                key,
                job_kind="ingest",
                job_id=job,
                job_attempt=attempt,
                send=send,
            )
        except ProviderBusy:
            messages.put(("busy", os.getpid(), conn.info.backend_pid))
        else:
            messages.put(("done", os.getpid(), conn.info.backend_pid))


def _stop(processes, release):
    # A killed child can leave an Event lock held; never touch it after all owners died.
    if any(process.pid is not None and process.is_alive() for process in processes):
        release.set()
    for process in processes:
        if process.pid is None:
            continue
        process.join(timeout=20)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        assert not process.is_alive()


def _resolve(item, key):
    with psycopg.connect(make_conninfo(item["admin"], user="kb_provisioner")) as conn:
        conn.execute(
            "SELECT kb.resolve_provider_call(%s,%s)",
            (key, "Synthetic operation resolved after owned process termination"),
        )


@pytest.fixture(autouse=True)
def _cleanup_owned_operations(world):
    yield
    with psycopg.connect(world["admin"]) as conn:
        keys = conn.execute(
            "SELECT call_key FROM kb.provider_call WHERE account_id=%s "
            "AND state IN ('running','unknown')",
            (world["principal"].account_id,),
        ).fetchall()
    for (key,) in keys:
        _resolve(world, key)


def _call(conn, item, job, attempt, key, send):
    return call_provider(
        conn,
        item["principal"],
        key,
        job_kind="ingest",
        job_id=job,
        job_attempt=attempt,
        send=send,
    )


def test_two_processes_share_one_provider_slot_without_open_io_transaction(world):
    ctx = mp.get_context("spawn")
    barrier, release, messages = ctx.Barrier(2), ctx.Event(), ctx.Queue()
    identity = (world["principal"].principal_id, world["principal"].account_id)
    processes = [
        ctx.Process(
            target=_send_in_process,
            args=(world["worker"], identity, *_job(world), _key(), barrier, release, messages),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        observed = [messages.get(timeout=20), messages.get(timeout=20)]
        assert sorted(item[0] for item in observed) == ["busy", "sending"]
        assert len({item[1] for item in observed}) == 2
        assert len({item[2] for item in observed}) == 2
        sender = next(item for item in observed if item[0] == "sending")
        with psycopg.connect(world["admin"]) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM kb.provider_call WHERE state='running'"
                ).fetchone()[0]
                == 1
            )
            state = conn.execute(
                "SELECT state,xact_start FROM pg_stat_activity WHERE pid=%s", (sender[2],)
            ).fetchone()
            assert state == ("idle", None)
        release.set()
        assert messages.get(timeout=20)[0] == "done"
    finally:
        _stop(processes, release)
    assert all(process.exitcode == 0 for process in processes)


def test_worker_loss_preserves_barrier_and_replay_needs_explicit_resolution(world):
    ctx = mp.get_context("spawn")
    release, messages = ctx.Event(), ctx.Queue()
    job, attempt = _job(world)
    other_job, other_attempt = _job(world)
    key, other_key = _key(), _key()
    identity = (world["principal"].principal_id, world["principal"].account_id)
    process = ctx.Process(
        target=_send_in_process,
        args=(world["worker"], identity, job, attempt, key, None, release, messages),
    )
    try:
        process.start()
        assert messages.get(timeout=20)[0] == "sending"
        process.terminate()
        process.join(timeout=10)
        assert not process.is_alive()

        def forbidden_send():
            raise AssertionError("provider must not be called")

        with psycopg.connect(world["worker"]) as conn:
            with pytest.raises(ProviderBusy):
                _call(conn, world, other_job, other_attempt, other_key, forbidden_send)
        with psycopg.connect(world["admin"]) as conn:
            assert (
                conn.execute(
                    "SELECT state FROM kb.provider_call WHERE call_key=%s", (key,)
                ).fetchone()[0]
                == "running"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(world["worker"]) as conn:
                conn.execute("SELECT kb.resolve_provider_call(%s,%s)", (key, "not authorised"))
        # A replacement epoch for the same job also cannot repeat an unresolved call.
        with psycopg.connect(world["admin"]) as conn:
            conn.execute(
                "UPDATE kb.job SET lease_until=now()-interval '1 second' WHERE id=%s", (job,)
            )
        with psycopg.connect(world["worker"], row_factory=psycopg.rows.dict_row) as conn:
            with transaction_identity(conn, world["principal"]):
                claimed, _ = _claim_job(conn, job, world["principal"])
                attempt = claimed["attempt_token"]
            with pytest.raises(ProviderReviewRequired):
                _call(conn, world, job, attempt, key, forbidden_send)
        _resolve(world, key)
        with psycopg.connect(world["worker"]) as conn:
            assert (
                _call(
                    conn,
                    world,
                    other_job,
                    other_attempt,
                    other_key,
                    lambda: httpx.Response(200, json={"synthetic": True}),
                ).status_code
                == 200
            )
            with pytest.raises(ProviderReviewRequired) as replay:
                _call(conn, world, job, attempt, key, forbidden_send)
            assert replay.value.code == "provider_operation_replay"
    finally:
        _stop([process], release)


@pytest.mark.parametrize("case", ["stale", "foreign", "app_role"])
def test_stale_foreign_or_unprivileged_claim_never_calls_provider(world, case):
    job, attempt = _job(world)
    item = {**world, "principal": world["other"]} if case == "foreign" else world
    epoch = uuid4() if case == "stale" else attempt
    dsn = world["dsn"] if case == "app_role" else world["worker"]
    expected = psycopg.errors.InsufficientPrivilege if case == "app_role" else AccessDenied

    def forbidden_send():
        raise AssertionError("forged claim reached provider")

    with psycopg.connect(dsn) as conn, pytest.raises(expected):
        _call(conn, item, job, epoch, _key(), forbidden_send)
    with psycopg.connect(world["admin"]) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM kb.provider_call WHERE account_id=%s",
                (world["principal"].account_id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("unknown", [False, True])
def test_transport_failure_distinguishes_known_not_started_from_unknown(world, unknown):
    job, attempt = _job(world)
    key = _key()

    def fail():
        if unknown:
            raise httpx.ReadTimeout("synthetic response lost")
        raise httpx.ConnectError("synthetic connection never started")

    with psycopg.connect(world["worker"]) as conn:
        with pytest.raises(ProviderReviewRequired if unknown else ProviderBusy):
            _call(conn, world, job, attempt, key, fail)
        with psycopg.connect(world["admin"]) as admin:
            row = admin.execute(
                "SELECT state FROM kb.provider_call WHERE call_key=%s", (key,)
            ).fetchone()
        assert row == ("unknown",) if unknown else row is None
        if not unknown:
            assert (
                _call(
                    conn,
                    world,
                    job,
                    attempt,
                    key,
                    lambda: httpx.Response(200, json={"synthetic": True}),
                ).status_code
                == 200
            )
