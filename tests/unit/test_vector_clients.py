"""The bounded SDK client pool: keys, bounds, lifecycle and the deadline.

These are unit tests on purpose. What has to be true here is about object
lifecycle and thread behaviour, and none of it needs a provider or a database
to check -- claiming otherwise would be the kind of test that passes because
it never exercised the thing it names.

Two properties are asserted over and over because they are the ones that
would be invisible in a run: a client is never closed while a call is still
in flight, and the credential never appears in a key, a repr or a bound.
"""

import threading
import time
from uuid import uuid4

import pytest

from kb.adapters.secrets import FileSecretSink
from kb.adapters.vector_clients import (
    Deadline,
    VectorClientCache,
    credential_revision,
)

URL = "https://vector.invalid"
OTHER_URL = "https://other.invalid"
KEY = "synthetic-read-key"
ROTATED = "synthetic-read-key-rotated"


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.initialized = False
        self.closed = False
        self.closes = 0
        self.find_calls = []

    def initialize(self):
        self.initialized = True

    def find(self, query, **kwargs):
        self.find_calls.append((query, kwargs))
        return {"resources": []}

    def close(self):
        self.closed = True
        self.closes += 1


@pytest.fixture
def store(tmp_path):
    """The sink root, and the read root the pool actually reads from.

    ``FileSecretSink`` lays a key out under a role directory, so the pool's
    read root is ``<sink root>/read`` -- the same layout the accepted
    ``OpenVikingReader`` fixture uses. Getting this wrong is a FileNotFound
    inside a try block, which would surface as a sanitised IndexUnavailable
    and look like an outage.
    """
    sink_root = tmp_path / "store"
    identities = []
    for _ in range(4):
        identity = f"kb-svc-read-{uuid4().hex}"
        FileSecretSink(sink_root).write(identity, KEY)
        identities.append(identity)
    return sink_root / "read", identities


def pool_for(read_root, built, **kwargs):
    def factory(**client_kwargs):
        client = FakeClient(**client_kwargs)
        built.append(client)
        return client

    return VectorClientCache(URL, read_root, factory, **kwargs)


def test_one_client_is_reused_across_calls_and_closed_on_shutdown(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    with cache.lease(identity):
        pass
    with cache.lease(identity):
        pass

    # The point of the pool: two calls, one client, one initialize.
    assert len(built) == 1
    assert built[0].initialized and not built[0].closed
    assert cache.read_key(identity)[0] == KEY

    cache.shutdown()
    assert built[0].closed
    # ``close`` is the same shutdown under the name the rest of the codebase
    # uses for an owned resource; both are idempotent.
    cache.close()
    assert built[0].closes == 1


def test_key_is_endpoint_identity_profile_and_credential_revision(store):
    read_root, identities = store
    first, second = identities[0], identities[1]
    built = []
    cache = pool_for(read_root, built)

    key = cache.key_for(first, credential_revision(KEY))
    assert key == (URL, first, key[2], credential_revision(KEY))
    assert (
        key[2]
        == __import__(
            "kb.retrieval.embedding_profile", fromlist=["SELECTED_PROFILE"]
        ).SELECTED_PROFILE.profile_hash
    )
    assert cache.key_for(second, credential_revision(KEY)) != key
    assert (
        VectorClientCache(OTHER_URL, read_root, lambda **k: None).key_for(
            first, credential_revision(KEY)
        )
        != key
    )
    assert cache.key_for(first, credential_revision(ROTATED)) != key


def test_raw_secret_is_never_a_key_a_bound_or_a_repr(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    with cache.lease(identity):
        pass

    assert KEY not in repr(cache)
    assert KEY not in str(cache.key_for(identity, credential_revision(KEY)))
    assert cache.read_key(identity) == (KEY, credential_revision(KEY))
    # The revision is a digest, and it is not the secret in any reversible form.
    assert credential_revision(KEY) != KEY
    assert len(credential_revision(KEY)) == 16
    assert credential_revision(KEY) == credential_revision(KEY)
    assert credential_revision(KEY) != credential_revision(ROTATED)
    # The one place the secret is allowed to appear is the SDK client itself.
    assert built[0].kwargs["api_key"] == KEY


def test_rotation_evicts_and_closes_the_old_client(store, tmp_path):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    with cache.lease(identity):
        pass
    first = built[0]

    FileSecretSink(tmp_path / "store").write(identity, ROTATED)
    with cache.lease(identity):
        pass

    assert len(built) == 2
    assert first.closed, "a rotated-away credential must close its client"
    assert built[1].kwargs["api_key"] == ROTATED
    assert not built[1].closed
    cache.shutdown()
    assert built[1].closed


def test_eviction_is_bounded_and_closes_what_it_drops(store):
    read_root, identities = store
    built = []
    cache = pool_for(read_root, built, max_clients=2)

    for identity in identities[:3]:
        with cache.lease(identity):
            pass
        assert len(cache) <= 2

    assert len(built) == 3
    # Bound is two, so exactly one entry was dropped: the oldest. The two that
    # survive are the most recent, and all three clients are accounted for.
    assert built[0].closed, "eviction must close, not leak"
    assert not built[1].closed and not built[2].closed
    assert len(cache) == 2
    cache.shutdown()
    assert built[1].closed and built[2].closed


def test_ttl_expiry_closes_the_stale_client(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    now = [1000.0]
    cache = pool_for(read_root, built, ttl_seconds=10.0, clock=lambda: now[0])

    with cache.lease(identity):
        pass
    assert not built[0].closed

    now[0] += 11.0
    with cache.lease(identity):
        pass
    assert built[0].closed
    assert len(built) == 2


def test_a_client_in_use_is_never_closed_under_the_caller(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    with cache.lease(identity) as (client, revision):
        assert revision == credential_revision(KEY)
        cache.shutdown()  # the process is going away mid-call
        assert not client.closed, "shutdown must not close under a live call"
    assert client.closed, "the last holder releases it, and then it closes"


def test_eviction_skips_a_busy_client_and_does_not_grow_past_the_bound(store):
    read_root, identities = store
    first, second = identities[0], identities[1]
    built = []
    cache = pool_for(read_root, built, max_clients=1)

    with cache.lease(first) as (busy, _):
        with cache.lease(second) as (spare, _):
            # Both calls are live and the bound is one. The pool refuses to
            # close either of them, so it is briefly over the bound rather
            # than landing a close on a call that is still running.
            assert len(cache) == 2
            assert not busy.closed
            assert not spare.closed
        # The inner call released first and became the idle one, so it is the
        # entry the bound is restored with.
        assert spare.closed
        assert not busy.closed

    assert len(cache) <= 1
    # The survivor is idle and still pooled: a pool of one that closed on every
    # release would be back to building a client per call.
    assert not busy.closed
    cache.shutdown()
    assert busy.closed


def test_expired_deadline_starts_nothing_and_says_so(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    # The clock has to actually move, or the budget never expires.
    now = [0.0]
    budget = Deadline(0.001, clock=lambda: now[0])
    now[0] = 1.0
    assert budget.expired

    with pytest.raises(TimeoutError) as raised:
        with cache.lease(identity, budget):
            pass
    assert str(raised.value) == "index_deadline_exceeded"
    assert built == [], "a call that cannot be waited for must not be started"
    assert len(cache) == 0


def test_the_budget_covers_the_queue_wait_and_the_network_ceiling(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built, network_timeout=45.0, max_outstanding=1)

    # One slot is taken by a call that is held open.
    held = threading.Event()
    release = threading.Event()

    def hold():
        with cache.lease(identity):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(5)

    # A second caller has to wait for the slot, and its budget is what bounds
    # that wait -- not a fixed sleep.
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with cache.lease(identity, Deadline(0.2)):
            pass
    waited = time.monotonic() - started
    assert 0.15 <= waited < 3.0, waited

    release.set()
    holder.join(5)
    assert not holder.is_alive()


def test_a_client_is_built_with_a_timeout_no_larger_than_the_budget(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built, network_timeout=45.0)
    now = [100.0]
    budget = Deadline(10.0, clock=lambda: now[0])
    now[0] += 2.0  # two seconds already spent on the plan

    with cache.lease(identity, budget):
        pass

    assert built[0].kwargs["timeout"] == pytest.approx(8.0)
    assert built[0].kwargs["timeout"] <= 45.0


def test_concurrent_callers_share_one_client_and_close_it_once(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)
    errors = []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait(5)
            with cache.lease(identity) as (client, _):
                client.find("q", target_uri="x", limit=1)
        except Exception as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert len(built) == 1, "one credential, one client, however many callers"
    cache.shutdown()
    assert built[0].closes == 1


def test_a_client_that_fails_to_close_does_not_fail_the_search(store):
    read_root, identities = store
    identity = identities[0]

    class Rude(FakeClient):
        def close(self):
            raise RuntimeError("close failed")

    def factory(**kwargs):
        return Rude(**kwargs)

    cache = VectorClientCache(URL, read_root, factory)
    with cache.lease(identity):
        pass
    cache.shutdown()  # must not raise


def test_deadline_rejects_a_nonsense_budget():
    for bad in (0, -1, -0.5, True, "5", None):
        with pytest.raises(ValueError):
            Deadline(bad)
    with pytest.raises(ValueError):
        credential_revision("")
