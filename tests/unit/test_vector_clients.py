"""The bounded SDK client pool: keys, bounds, lifecycle and the deadline.

These are unit tests on purpose. What has to be true here is about object
lifecycle and thread behaviour, and none of it needs a provider or a database
to check -- claiming otherwise would be the kind of test that passes because
it never exercised the thing it names.

Three properties are asserted over and over because they are the ones that
would be invisible in a run: a request cannot outlive its own budget on a
client some other request built, a client is never closed while a call is
still in flight, and the credential never appears in a key, a repr or a bound.

The deadline tests do not stub the transport. A double cannot show whether a
cancel reached httpx, and the whole point of the repair is that it does, so
the ones that matter run against a real socket on the loopback interface that
accepts a request and then does whatever the test told it to do.
"""

import asyncio
import base64
import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import pytest
from openviking_sdk import AsyncHTTPClient

import kb.adapters.vector_clients as vector_clients
from kb.adapters.secrets import FileSecretSink
from kb.adapters.vector_clients import (
    Deadline,
    IndexCallStillOpen,
    VectorClientCache,
    VectorClientPoolClosed,
    credential_revision,
)

URL = "https://vector.invalid"
OTHER_URL = "https://other.invalid"
KEY = "synthetic-read-key"
ROTATED = "synthetic-read-key-rotated"
FIND_PATH = b"POST /api/v1/search/find HTTP/1.1"


class FakeClient:
    """A stand-in with the *shape* of the SDK client: coroutine methods.

    Async is the point. The real ``AsyncHTTPClient`` builds, calls and closes
    on an event loop, and a synchronous double would let a bug that only
    shows up against the real coroutine methods pass here.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.initialized = False
        self.closed = False
        self.closes = 0
        self.find_calls = []

    async def initialize(self):
        self.initialized = True

    async def find(self, query, **kwargs):
        self.find_calls.append((query, kwargs))
        return {"resources": []}

    async def close(self):
        self.closed = True
        self.closes += 1


class IndexServer:
    """A real HTTP server on loopback that follows a per-request script.

    ``answer`` sends a valid ``find`` response. ``silent`` reads the request
    and never replies, which is what a provider that has stopped responding
    looks like from this side. Every request on a connection is counted, so a
    test can tell which one it is looking at, and a connection that is torn
    down sets ``broken`` -- which is how a client-side cancel is observed from
    the peer's side rather than taken on trust.
    """

    def __init__(self, script=("answer",)):
        self._script = list(script)
        self._lock = threading.Lock()
        self.requests = 0
        self.broken = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._sock.getsockname()[:2]
        return f"http://{host}:{port}"

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _next_mode(self) -> str:
        with self._lock:
            self.requests += 1
            return self._script.pop(0) if self._script else "answer"

    def _serve(self, conn):
        body = b'{"result":{"resources":[]}}'
        conn.settimeout(30.0)
        buffer = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    # The peer hung up. A connection that only ever carried a
                    # silent request was cancelled by its client.
                    self.broken.set()
                    return
                buffer += chunk
                while FIND_PATH in buffer:
                    buffer = buffer.split(FIND_PATH, 1)[1]
                    if self._next_mode() == "answer":
                        conn.sendall(
                            b"HTTP/1.1 200 OK\r\n"
                            b"content-type: application/json\r\n"
                            b"content-length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                        )
        except OSError:
            # A reset is the other face of a close: either way this connection
            # is gone, which is the fact the cancel test is looking for.
            self.broken.set()

    def close(self):
        try:
            self._sock.close()
        except OSError:  # pragma: no cover - already closed
            pass


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


def _try_lease(cache, identity, budget, result):
    """Lease once and record the outcome as a bare word."""
    try:
        with cache.lease(identity, budget):
            result.append("admitted")
    except VectorClientPoolClosed:
        result.append("VectorClientPoolClosed")
    except TimeoutError:
        result.append("timeout")
    except Exception as exc:
        result.append(type(exc).__name__)


def wait_flag(owner, name, timeout=2.0):
    """Wait briefly for an attribute on a double to become truthy.

    The cancel is scheduled before the caller returns and observed by the SDK
    coroutine a moment later, so "did it see it" is a question about a
    delivered event and not about the instant the lease raised.
    """
    end = time.monotonic() + timeout
    while not getattr(owner, name, False) and time.monotonic() < end:
        time.sleep(0.005)
    return bool(getattr(owner, name, False))


def wait_event(event, timeout=2.0):
    """The same wait for a flag that is already an Event."""
    end = time.monotonic() + timeout
    while not event.is_set() and time.monotonic() < end:
        time.sleep(0.005)
    return event.is_set()


def wait_closed(clients, timeout=5.0):
    """Wait for handed-off closes to actually land. Returns whether they did.

    Closes no longer run on the caller's thread: that spent a caller's budget
    on another client's teardown. They are owned, counted, and guaranteed, but
    a test that asserted ``closed`` the instant a lease returned was asserting
    a race. These wait for the completion the pool actually promises.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(client.closed for client in clients):
            return True
        time.sleep(0.001)
    return all(client.closed for client in clients)


def pool_for(read_root, built, **kwargs):
    def factory(**client_kwargs):
        client = FakeClient(**client_kwargs)
        built.append(client)
        return client

    return VectorClientCache(URL, read_root, factory, **kwargs)


def wait_idle(cache, timeout=5.0):
    """Wait for the I/O loop to actually stop, if it is going to."""
    deadline = time.monotonic() + timeout
    while cache.io_loop_running and time.monotonic() < deadline:
        time.sleep(0.001)
    return not cache.io_loop_running


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
    # Deterministic, not eventual: the loop that drove every client is gone
    # and no new one is started behind the caller's back.
    assert not cache.io_loop_running
    with pytest.raises(VectorClientPoolClosed):
        with cache.lease(identity):
            pass


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
    assert wait_closed([first]), "a rotated-away credential must close its client"
    assert built[1].kwargs["api_key"] == ROTATED
    assert not built[1].closed
    cache.shutdown()
    assert wait_closed([built[1]])


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
    assert wait_closed([built[0]]), "eviction must close, not leak"
    assert not built[1].closed and not built[2].closed
    assert len(cache) == 2
    cache.shutdown()
    assert wait_closed([built[1], built[2]])


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
    assert wait_closed([built[0]])
    assert len(built) == 2


def test_a_client_in_use_is_never_closed_under_the_caller(store):
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    with cache.lease(identity) as leased:
        assert leased.revision == credential_revision(KEY)
        cache.shutdown()  # the process is going away mid-call
        assert not built[0].closed, "shutdown must not close under a live call"
    assert wait_closed([built[0]]), "the last holder releases it, and then it closes"
    # The loop is only stopped once the last holder is gone AND that holder's
    # close has actually landed, so the close above had a loop to run on.
    assert wait_idle(cache)


def test_a_busy_client_is_never_closed_and_the_cap_still_holds(store):
    """A client in use is never closed, and the owned cap is not exceeded.

    The cap on *owned* clients used to cover only what the pool held, so a pool
    of one would briefly hold two: it preferred a live client over its own
    bound. Now the bound counts clients that exist at all, so a second key is
    refused while the first client is live and admitted once that client has
    been released and closed. Refusing is the safe direction -- the alternative
    was more live SDK clients than the pool said it owned.
    """
    read_root, identities = store
    first, second = identities[0], identities[1]
    built = []
    cache = pool_for(read_root, built, max_clients=1)

    # The first client stays live for the whole attempt, which is the only state
    # in which nothing can be dropped to make room.
    with cache.lease(first) as leased:
        leased.find("q", target_uri="x", limit=1)
        with pytest.raises(TimeoutError):
            with cache.lease(second, Deadline(0.2)) as other:
                other.find("q", target_uri="x", limit=1)
        assert not built[0].closed, "a busy client must not be closed to make room"
        assert len(built) == 1, "and no replacement may be built either"
        assert len(cache) == 1

    # Once it has released, there is room. The second key drops the idle client,
    # whose close hands its permit back, so the pool still owns exactly one.
    with cache.lease(second, Deadline(5.0)) as other:
        other.find("q", target_uri="x", limit=1)
    assert wait_closed([built[0]])
    assert len(built) == 2
    assert len(cache) == 1
    cache.shutdown()
    assert wait_closed(built)


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


def test_a_pooled_client_timeout_is_a_fixed_ceiling_and_never_a_requests_budget(store):
    """The defect this pool exists to answer, stated as an invariant.

    A client is shared. If its socket timeout were derived from the budget of
    whichever request happened to build it, then the next request would
    inherit that request's budget -- a client cached under a large one would
    make a short-budget request wait for the large one. So the timeout is a
    property of the client and of nothing else.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    now = [100.0]
    cache = pool_for(read_root, built, network_timeout=45.0, clock=lambda: now[0])

    # A caller with most of a minute left.
    with cache.lease(identity, Deadline(50.0, clock=lambda: now[0])):
        pass
    # A caller whose budget is nearly gone, on a *different* identity so the
    # second build is a genuinely new client.
    other = identities[1]
    nearly_gone = Deadline(1.0, clock=lambda: now[0])
    now[0] += 0.99
    with cache.lease(other, nearly_gone):
        pass

    assert built[0].kwargs["timeout"] == 45.0
    assert built[1].kwargs["timeout"] == 45.0, "a budget must not shape a shared client"
    assert cache.read_key(identity)[0] == KEY


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
            with cache.lease(identity) as leased:
                leased.find("q", target_uri="x", limit=1)
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
        async def close(self):
            raise RuntimeError("close failed")

    def factory(**kwargs):
        return Rude(**kwargs)

    cache = VectorClientCache(URL, read_root, factory)
    with cache.lease(identity):
        pass
    cache.shutdown()  # must not raise


def test_a_budget_that_is_gone_before_the_call_sends_nothing(store):
    """No budget, no request -- and said plainly, not as a stuck teardown.

    The budget can expire in the gap between taking the lease and reaching the
    wire. Creating a task and cancelling it there would leave a coroutine that
    never ran a line of its body, so the "did it stop?" question has no honest
    answer to give; sending nothing does.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)
    now = [0.0]
    budget = Deadline(5.0, clock=lambda: now[0])

    with cache.lease(identity, budget) as leased:
        now[0] = 10.0  # the earlier phases took the whole budget
        assert budget.expired
        with pytest.raises(TimeoutError) as raised:
            leased.find("q", target_uri="x", limit=1)

    assert str(raised.value) == "index_deadline_exceeded"
    assert built[0].find_calls == [], "a call with no budget must not reach the client"
    assert cache.io_loop_running
    cache.shutdown()
    assert not cache.io_loop_running


def test_a_call_that_refuses_to_unwind_is_reported_rather_than_called_a_timeout(store):
    """A cancellation we asked for and did not get is its own fact.

    Dressing it up as a timeout would tell the caller the wire was clear when
    this module cannot show that it is.
    """
    read_root, identities = store
    identity = identities[0]

    class Deaf(FakeClient):
        async def find(self, query, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                # Refuses the cancellation for longer than the grace.
                await asyncio.sleep(0.2)
                raise
            return {"resources": []}

    def factory(**kwargs):
        return Deaf(**kwargs)

    cache = VectorClientCache(URL, read_root, factory, cancel_grace=0.05)
    with cache.lease(identity, Deadline(0.3)) as leased:
        with pytest.raises(IndexCallStillOpen):
            leased.find("q", target_uri="x", limit=1)
    # Not a TimeoutError: the two are different facts and the caller closes over
    # the socket differently for each.
    now = [0.0]
    gone = Deadline(0.001, clock=lambda: now[0])
    now[0] = 5.0
    with pytest.raises(TimeoutError):
        with cache.lease(identity, gone):
            pass
    time.sleep(0.3)  # let the deaf coroutine finish so the loop stops cleanly
    cache.shutdown()


def test_deadline_rejects_a_nonsense_budget():
    for bad in (0, -1, -0.5, True, "5", None):
        with pytest.raises(ValueError):
            Deadline(bad)
    with pytest.raises(ValueError):
        credential_revision("")


# -- the two repairs, against the real SDK client and a real socket --------


def test_a_cached_client_with_a_larger_timeout_does_not_outlive_a_shorter_budget(store):
    """The frozen criterion, end to end and with nothing faked.

    One client is cached by a caller with a generous budget. A second caller
    arrives with most of its budget already spent on earlier phases, and the
    provider stops responding. The second call must return at *its* deadline
    rather than at the 45 second socket timeout the cached client was built
    with, the request must actually be torn down rather than abandoned, and
    the lease must not be left dangling.
    """
    read_root, identities = store
    identity = identities[0]
    server = IndexServer(script=("answer", "silent", "answer"))
    built: list[dict] = []

    def factory(**client_kwargs):
        built.append(client_kwargs)
        return AsyncHTTPClient(**client_kwargs)

    # One outstanding call at a time, so the third call below is also proof
    # that the second one's slot was handed back rather than leaked.
    cache = VectorClientCache(
        server.url, read_root, factory, network_timeout=45.0, max_outstanding=1
    )
    try:
        with cache.lease(identity, Deadline(20.0)) as leased:
            assert leased.find("first", target_uri="x", limit=1) == {"resources": []}
        assert built[0]["timeout"] == 45.0, "the pooled client is the fixed ceiling"

        # A second caller whose budget is nearly spent before the network call
        # is even attempted: the queue and the earlier phases ate it.
        budget = Deadline(2.0)
        time.sleep(1.5)
        remaining = budget.remaining
        assert 0.0 < remaining < 1.0

        started = time.monotonic()
        with cache.lease(identity, budget) as leased:
            with pytest.raises(IndexCallStillOpen) as raised:
                leased.find("second", target_uri="x", limit=1)
        elapsed = time.monotonic() - started
        # Reported unfinished rather than timed out, because the request no
        # longer waits out a cancel grace to find out: the submission is handed
        # to the pool and the caller returns at its own deadline. What this
        # test is about is unaffected -- the call still ended at the budget and
        # not at the cached client's 45 second socket timeout.
        assert "did not stop" in str(raised.value)
        # The whole point: bounded by the budget, not by the 45s socket
        # timeout the cached client carries, and not by a fixed sleep either.
        assert elapsed < 5.0, f"outlived the budget by {elapsed:.1f}s"
        assert elapsed >= remaining - 0.05

        # And the request was actually stopped: the peer saw the connection go
        # away, which a caller that merely stopped waiting would not cause.
        assert server.broken.wait(5.0), "the in-flight request was left on the wire"
        assert server.requests == 2

        # The client is still pooled and still usable, which also proves the
        # outstanding slot came back: a leaked one would block this forever.
        with cache.lease(identity, Deadline(20.0)) as leased:
            assert leased.find("third", target_uri="x", limit=1) == {"resources": []}
        # The cancelled client was quarantined, because its request was still
        # open when the budget ran out, so the third call is served by a fresh
        # one -- still on the same fixed ceiling, never on a per-request budget.
        assert len(cache) == 1
        assert len(built) == 2
        assert [b["timeout"] for b in built] == [45.0, 45.0]
    finally:
        cache.shutdown()
        server.close()
    assert not cache.io_loop_running


def test_the_gateway_lifespan_closes_cached_clients_and_stops_the_io_loop(
    store, monkeypatch, tmp_path
):
    """Shutdown is a fact the app reaches, not a hook that may or may not run.

    The whole real ``create_app`` runs -- the real lifespan, the real
    ``SemanticSearch``, the real pool, the real event loop. Only the database
    and the provider client are replaced, because neither can be reached from
    a test: the pool double satisfies the profile-dimension probe, and the
    recording client records its own close.
    """
    from fastapi.testclient import TestClient

    import kb.gateway
    from kb.adapters.vector_reader import OpenVikingReader
    from kb.retrieval.embedding_profile import SELECTED_PROFILE

    read_root, identities = store
    identity = identities[0]
    built = []

    class RecordingClient(FakeClient):
        async def find(self, query, **kwargs):
            self.find_calls.append((query, kwargs))
            return {"resources": []}

    class Pool:
        def __init__(self, *args, **kwargs):
            self.opened = False

        def open(self, wait=False):
            self.opened = True

        def close(self):
            self.opened = False

    class Conn:
        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return (SELECTED_PROFILE.dimension,)

    @contextmanager
    def connections():
        yield Conn()

    class Reader(OpenVikingReader):
        def __init__(self, url, keys_root, *args, **kwargs):
            def factory(**client_kwargs):
                client = RecordingClient(**client_kwargs)
                built.append(client)
                return client

            super().__init__(url, keys_root, factory)

    for key, value in {
        "KB_ALLOWED_HOSTS": "gateway.example",
        "KB_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",
        "KB_MCP_AUDIENCE": "mcp",
        "KB_MCP_CLIENT_ID": "mcp-client",
        "KB_MCP_RESOURCE_URL": "https://gateway.example/mcp",
        "KB_OIDC_ISSUER": "https://id.example/realms/kb",
        "KB_OIDC_AUDIENCE": "kb-web",
        "KB_OIDC_CLIENT_ID": "kb-web-client",
        "KB_OIDC_CLIENT_SECRET": "placeholder-not-a-secret",
        "KB_IDENTITY_KEY": base64.urlsafe_b64encode(b"\x00" * 32).decode(),
        "KB_OIDC_REDIRECT_URI": "https://gateway.example/auth/callback",
        "KB_OIDC_JWKS_URL": "https://id.example/jwks",
        "KB_BLOB_ROOT": str(tmp_path),
        "KB_SEMANTIC_SEARCH_ENABLED": "1",
        "KB_OPENVIKING_URL": "https://vector.invalid",
        "KB_READ_KEYS_ROOT": str(read_root),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(kb.gateway, "ConnectionPool", Pool)
    monkeypatch.setattr(kb.gateway, "build_connection_factory", lambda pool: connections)
    monkeypatch.setattr(kb.gateway, "OpenVikingReader", Reader)

    with TestClient(kb.gateway.create_app(), base_url="http://gateway.example") as client:
        service = client.app.state.semantic_search
        assert service is not None
        # A real search: the client is built on the real loop and pooled.
        pool = service.reader.pool
        with pool.lease(identity) as leased:
            assert leased.find("q", target_uri="x", limit=1) == {"resources": []}
        assert len(built) == 1
        assert pool.io_loop_running
        assert not built[0].closed

    # Leaving the lifespan is the deterministic shutdown.
    assert built[0].closed, "lifespan shutdown must close the cached clients"
    assert pool.is_shutdown
    assert not pool.io_loop_running, "the I/O loop thread must be joined, not leaked"
    assert built[0].closes == 1


def test_the_outstanding_slot_comes_back_after_an_ordinary_lease(store):
    """The regression that matters most: one lease must not exhaust the pool.

    A semaphore release hidden behind the failure path costs the pool its
    second caller, and nothing notices until something needs real concurrency
    -- which is exactly what a provider call is. The budget here is generous
    so a leaked slot shows up as a failure rather than as a hung suite.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built, max_outstanding=1)

    for _ in range(3):
        with cache.lease(identity, Deadline(2.0)) as leased:
            leased.find("q", target_uri="x", limit=1)

    assert len(built) == 1, "three serial leases, one client"
    cache.shutdown()


def test_a_contended_pool_lock_does_not_spend_more_than_the_budget(store):
    """The lock is where every caller queues; it is inside the budget now.

    Another thread is holding the pool lock for far longer than this caller's
    whole budget. The lease must come back as a timeout at its own deadline,
    not when the lock happens to be released.
    """
    read_root, identities = store
    built = []
    cache = pool_for(read_root, built)
    outcome = []
    finished = threading.Event()

    def acquire():
        try:
            with cache.lease(identities[0], Deadline(0.05)) as leased:
                leased.find("q", target_uri="x", limit=1)
            outcome.append("admitted")
        except TimeoutError:
            outcome.append("timeout")
        finally:
            finished.set()

    with cache._lock:  # a second thread is inside the pool
        worker = threading.Thread(target=acquire, daemon=True)
        worker.start()
        returned_early = finished.wait(1.0)
        still_held = list(outcome)

    worker.join(5)
    assert returned_early, "the lease waited for the lock past its own budget"
    assert still_held == ["timeout"]
    assert not worker.is_alive()
    cache.shutdown()


def test_a_credential_read_that_overruns_keeps_its_slot_until_it_finishes(store):
    """A read that outlives the budget is a timeout, and still owns its slot.

    The caller is released at its own deadline and no client is built, because
    starting SDK work after expiry is the thing the whole repair is about. The
    read itself is not abandoned: it keeps the slot it was charged, so the
    outstanding bound still counts it, and the slot comes back when it ends.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built, max_outstanding=1)
    entered, release = threading.Event(), threading.Event()

    def blocked_read(_identity):
        entered.set()
        release.wait(5)
        return KEY, credential_revision(KEY)

    cache.read_key = blocked_read
    outcome = []

    def acquire(budget):
        try:
            with cache.lease(identity, budget) as leased:
                leased.find("q", target_uri="x", limit=1)
            outcome.append("admitted")
        except TimeoutError:
            outcome.append("timeout")

    first = threading.Thread(target=acquire, args=(Deadline(0.05),), daemon=True)
    first.start()
    assert entered.wait(5)
    first.join(5)
    assert outcome == ["timeout"]
    assert built == [], "no client may be built after the budget expired"

    # Admission, not completion, is the question: a second caller must be
    # turned away while the first one's read still owns the only slot.
    second_admitted = threading.Event()
    second_done = threading.Event()

    def acquire_again(budget, admitted):
        try:
            with cache.lease(identity, budget) as leased:
                leased.find("q", target_uri="x", limit=1)
            admitted.set()
        except TimeoutError:
            pass
        second_done.set()

    while_read = threading.Thread(
        target=acquire_again, args=(Deadline(0.05), second_admitted), daemon=True
    )
    while_read.start()
    assert second_done.wait(5)
    assert not second_admitted.is_set(), "the retained read must still hold its only slot"
    while_read.join(5)

    # And the slot does come back, so the pool is not permanently one deep.
    release.set()
    after = threading.Event()
    recovered = threading.Thread(target=acquire_again, args=(Deadline(5.0), after), daemon=True)
    recovered.start()
    recovered.join(10)
    assert after.is_set(), "the slot must come back when the retained read finishes"
    cache.shutdown()


def test_a_slow_initialize_is_cancelled_at_the_deadline(store):
    """Initialization is inside the budget, and cancellation is real.

    A client whose initialization is not finished is not a usable client, so
    it is closed rather than pooled, and the budget is what stops it -- not the
    fixed close ceiling.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    started, cancelled = threading.Event(), threading.Event()

    class SlowInit(FakeClient):
        async def initialize(self):
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    def factory(**kwargs):
        client = SlowInit(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, factory, cancel_grace=1.0)
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        with cache.lease(identity, Deadline(0.05)) as leased:
            leased.find("q", target_uri="x", limit=1)
    elapsed = time.monotonic() - began

    assert started.is_set(), "initialization must actually begin"
    assert wait_event(cancelled), "a cancelled initialize must be told, not dropped"
    assert elapsed < 2.0, elapsed
    cache.shutdown()
    assert built[0].closed, "a half-built client must be closed, not leaked"


def test_a_close_in_progress_does_not_make_another_caller_wait_for_it(store, tmp_path):
    """A close is this pool's housekeeping, and it is not the lock's business.

    Rotating the credential makes this caller's own acquisition retire and
    close the previous client. That close is deliberately slow, and an
    unrelated caller arriving meanwhile must not be made to wait for it.
    """
    read_root, identities = store
    first, second = identities[0], identities[1]
    started, release = threading.Event(), threading.Event()

    class SlowClose(FakeClient):
        async def close(self):
            started.set()
            # The blocking wait is handed to a thread and awaited, so the event
            # loop keeps turning. A close test that wedges the loop on purpose
            # would be testing the wrong thing: the pool is right to time out
            # against a loop that cannot answer.
            await asyncio.to_thread(release.wait, 5)
            self.closed = True
            self.closes += 1

    cache = VectorClientCache(URL, read_root, lambda **kwargs: SlowClose(**kwargs))
    with cache.lease(first) as leased:
        leased.find("q", target_uri="x", limit=1)

    FileSecretSink(tmp_path / "store").write(first, ROTATED)
    finished = threading.Event()

    def rotate():
        # A generous budget: what is being measured is the wait for the close,
        # not the budget.
        with cache.lease(first, Deadline(10.0)) as leased:
            leased.find("q", target_uri="x", limit=1)
        finished.set()

    worker = threading.Thread(target=rotate, daemon=True)
    worker.start()
    assert started.wait(5), "the rotated-away client should be closing now"

    began = time.monotonic()
    with cache.lease(second, Deadline(2.0)) as leased:
        leased.find("q", target_uri="x", limit=1)
    waited = time.monotonic() - began
    assert waited < 1.0, f"an unrelated close delayed a caller by {waited:.2f}s"

    release.set()
    assert finished.wait(5)
    worker.join(5)
    cache.shutdown()


def test_shutdown_accounts_for_an_acquisition_still_reading_its_credential(store):
    """An acquisition with no entry yet is still work the pool started.

    Shutdown used to count holders only, so a credential read that outlived
    its caller's budget was invisible to it: the loop would be stopped while
    that read was still running.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)
    entered, release = threading.Event(), threading.Event()

    def blocked_read(_identity):
        entered.set()
        release.wait(5)
        return KEY, credential_revision(KEY)

    cache.read_key = blocked_read
    outcome = []

    def acquire():
        try:
            with cache.lease(identity, Deadline(0.05)):
                pass
            outcome.append("admitted")
        except TimeoutError:
            outcome.append("timeout")

    worker = threading.Thread(target=acquire, daemon=True)
    worker.start()
    assert entered.wait(5)
    worker.join(5)
    assert outcome == ["timeout"]
    # Charged twice, because two things are true at once: the acquisition still
    # holds its slot and its registration, and the pool owns the read itself.
    # They are handed back together by the same owner, which is the property
    # that matters; the exact count is an implementation detail.
    assert cache._outstanding_work() >= 1, "shutdown must see the outstanding read"

    stopper = threading.Event()

    def stop():
        cache.shutdown()
        stopper.set()

    reaper = threading.Thread(target=stop, daemon=True)
    reaper.start()
    release.set()
    reaper.join(10)
    assert stopper.is_set(), "shutdown did not return once the read ended"
    assert not cache.io_loop_running
    cache.shutdown()


def test_a_close_is_counted_before_the_pool_lock_is_released(store):
    """The ordering the review called out, pinned without racing.

    A close is chosen while the pool lock is held, so it has to be counted
    there too. Counting it after the unlock left a window in which a shutdown
    arriving in between saw every counter at zero and stopped the I/O loop
    under a close that had been chosen but not yet started.

    The close blocks on an event, so the charge cannot drain away between the
    registration and the assertion. Without that this test was measuring how
    fast the closer thread happened to run, which is not the contract.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    release_close = threading.Event()

    class BlockingClose(FakeClient):
        async def close(self):
            await asyncio.to_thread(release_close.wait, 5)
            self.closed = True
            self.closes += 1

    def make(**kwargs):
        client = BlockingClose(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, make)
    with cache.lease(identity) as leased:
        leased.find("q", target_uri="x", limit=1)

    observed = {}

    def dropper():
        with cache._lock:
            closers: list = []
            for entry in list(cache._entries.values()):
                cache._drop_locked(entry, closers)
            # Where the pool counts it: still under the lock.
            cache._close_all(closers)
            observed["under_lock"] = cache._outstanding_work()
        observed["after_unlock"] = cache._outstanding_work()

    worker = threading.Thread(target=dropper, daemon=True)
    worker.start()
    worker.join(5)

    assert observed.get("under_lock", 0) >= 1, (
        "a close chosen under the lock must be counted before it is released"
    )
    release_close.set()
    assert wait_closed([built[0]])
    cache.shutdown()


def test_a_close_handoff_that_cannot_start_is_handed_back(store, monkeypatch):
    """A closer that never starts must not strand the drain count.

    If the reservation is not given back, the counter never returns to zero,
    the pool never drains again, and the clients it was holding are never
    closed. Both of those are worse than the close failing.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)
    with cache.lease(identity) as leased:
        leased.find("q", target_uri="x", limit=1)

    def refuse(_target):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr("kb.adapters.vector_clients._start_closer", refuse)
    for entry in list(cache._entries.values()):
        with cache._critical(None) as chosen:
            cache._drop_locked(entry, chosen)
            cache._close_all(chosen)

    assert cache._outstanding_work() == 0, "the reservation must be handed back"
    cache.shutdown()
    assert not cache.io_loop_running, "a failed handoff must not strand the pool"


def test_shutdown_does_not_stop_the_loop_under_a_close_in_flight(store):
    """The loop outlives a close that is really running, and is stopped after.

    The behaviour end to end, through the pool's own release path: a slow close
    is in flight, a shutdown arrives, and it has to find the close and wait for
    it rather than reporting a drained pool and stopping the loop underneath.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    started, release = threading.Event(), threading.Event()

    class SlowClose(FakeClient):
        async def close(self):
            started.set()
            await asyncio.to_thread(release.wait, 5)
            self.closed = True
            self.closes += 1

    def make(**kwargs):
        client = SlowClose(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, make)
    with cache.lease(identity) as leased:
        leased.find("q", target_uri="x", limit=1)

    # shutdown itself drops the idle entry, so the close is on the pool's path.
    returned = threading.Event()

    def stop():
        cache.shutdown()
        returned.set()

    stopper = threading.Thread(target=stop, daemon=True)
    stopper.start()
    assert started.wait(5), "the idle client should be closing"

    stopper.join(0.2)
    assert not returned.is_set(), "shutdown must wait for a close that is running"
    assert cache.io_loop_running, "the loop must survive while a close is on it"

    release.set()
    stopper.join(10)
    assert wait_closed([built[0]])
    assert wait_idle(cache)


def test_a_close_that_refuses_to_stop_is_owned_until_it_really_ends(store, monkeypatch):
    """B1-close-unfinished-ownership: a cancelled close is not a close.

    A close that ignores its cancellation and stays unfinished through the
    grace is still holding the socket. If the pool treats the refusal as a
    finished close it will go on to stop the I/O loop underneath a coroutine
    that is still running.
    """
    monkeypatch.setattr(vector_clients, "CLOSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(vector_clients, "IO_JOIN_SECONDS", 0.05)
    read_root, identities = store
    identity = identities[0]
    built = []

    class DeafClose(FakeClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.close_started = threading.Event()
            self.close_release = threading.Event()
            self.cancelled = threading.Event()

        async def close(self):
            self.close_started.set()
            try:
                await asyncio.to_thread(self.close_release.wait, 5)
            except asyncio.CancelledError:
                # Refuses the cancellation outright, which is the case: a close
                # that merely unwound on cancel would have finished, and this
                # test would prove nothing. This flag is what proves the
                # cancellation actually reached the SDK coroutine.
                self.cancelled.set()
                await asyncio.to_thread(self.close_release.wait, 5)
                raise
            self.closed = True
            self.closes += 1

    def make(**kwargs):
        client = DeafClose(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, make, cancel_grace=0.05)
    with cache.lease(identity) as leased:
        leased.find("q", target_uri="x", limit=1)

    # shutdown drops the idle entry, and its close is the one that refuses.
    stopper = threading.Event()

    def stop():
        cache.shutdown()
        stopper.set()

    worker = threading.Thread(target=stop, daemon=True)
    worker.start()
    assert built[0].close_started.wait(5), "the close should have started"
    assert stopper.wait(5), "shutdown should return having reported work outstanding"

    # The refusal is still running, so it is still owned: not closed, counted,
    # and the loop it is sitting on is still alive.
    assert not built[0].closed, "a refused close must not be reported as closed"
    assert cache._outstanding_work() >= 1, "an unfinished close must stay charged"
    assert cache.io_loop_running, "the loop must outlive a close that has not stopped"

    # The real unwind releases the charge and finalises the pool. A close that
    # was cancelled did not close, so the client is dropped unclosed -- counted
    # and reported rather than quietly forgotten.
    built[0].close_release.set()
    deadline = time.monotonic() + 5
    while (cache.io_loop_running or cache._abandoned_closes == 0) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert wait_flag(built[0], "cancelled"), "the cancellation must reach the SDK coroutine"
    assert cache._abandoned_closes == 1, "a cancelled close is not a close, and says so"
    assert wait_idle(cache)
    assert cache._finalized


def test_an_initialize_that_refuses_cancellation_keeps_its_charge(store):
    """B1-init-unfinished-ownership: do not close a client mid-initialization.

    A client whose initialize is still running is a client in the middle of
    becoming one. Closing it now races that coroutine, and the client can end
    up initialized after the only close meant to catch it. Its capacity has to
    stay charged for the whole time, or the pool admits more work than its
    bound allows while that initialize is still going.
    """
    read_root, identities = store
    identity = identities[0]
    built = []

    class DeafInit(FakeClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.init_started = threading.Event()
            self.init_release = threading.Event()
            self.init_finished = threading.Event()

        async def initialize(self):
            self.init_started.set()
            try:
                await asyncio.to_thread(self.init_release.wait, 5)
            except asyncio.CancelledError:
                # Refuses, so the initialize is still running after its budget.
                await asyncio.to_thread(self.init_release.wait, 5)
                raise
            finally:
                self.init_finished.set()

    def make(**kwargs):
        client = DeafInit(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, make, cancel_grace=0.05, max_outstanding=1)
    with pytest.raises(TimeoutError):
        with cache.lease(identity, Deadline(0.05)) as leased:
            leased.find("q", target_uri="x", limit=1)

    assert built[0].init_started.is_set(), "initialize must actually have begun"
    assert not built[0].closed, "must not close a client that is still initializing"
    assert cache._outstanding_work() >= 1, "the unfinished initialize stays charged"

    # One slot, still held by the first acquisition, so a second caller is
    # turned away rather than the pool quietly running one over its bound.
    admitted = threading.Event()

    def second():
        try:
            with cache.lease(identity, Deadline(0.05)):
                admitted.set()
        except TimeoutError:
            pass

    other = threading.Thread(target=second, daemon=True)
    other.start()
    other.join(5)
    assert not admitted.is_set(), "capacity must be held while the initialize runs"

    # The real unwind is what releases everything, and the close comes after it,
    # never racing it.
    built[0].init_release.set()
    assert built[0].init_finished.wait(5)
    assert wait_closed(built)
    # Polled, not asserted once: the coroutine finishing and the owner handing
    # its charge back are two different instants, and the second is the one
    # being checked here.
    deadline = time.monotonic() + 5
    while cache._outstanding_work() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cache._outstanding_work() == 0, "the charge is released when it unwinds"
    # This pool was never shut down, so the finalizer must not have run and the
    # loop must still be there for the next request.
    assert not cache._finalized
    cache.shutdown()
    assert wait_idle(cache)


def test_work_finishing_after_shutdown_returns_finalises_the_whole_pool(store, monkeypatch):
    """B1-late-shutdown-finalizer: one finalizer, and it stops everything.

    When shutdown's bounded wait expires it says so and returns, and whatever
    was still running gets to finish afterwards. That late completion has to
    complete the teardown -- the I/O loop *and* the credential executor -- or
    the pool is left half shut, with a worker alive until some later shutdown
    or the interpreter.
    """
    monkeypatch.setattr(vector_clients, "IO_JOIN_SECONDS", 0.05)
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    # Warm the pool first, so there is an I/O loop for shutdown to have to leave
    # alone. A pool that never built a client has no loop, and asserting one is
    # still running there would be asserting nothing.
    with cache.lease(identity) as leased:
        leased.find("q", target_uri="x", limit=1)
    assert cache.io_loop_running

    entered, release = threading.Event(), threading.Event()

    def blocked_read(_identity):
        entered.set()
        release.wait(5)
        return KEY, credential_revision(KEY)

    cache.read_key = blocked_read
    with pytest.raises(TimeoutError):
        with cache.lease(identity, Deadline(0.05)):
            pass
    assert entered.wait(5), "the credential read should have started"

    stopper = threading.Event()

    def stop():
        cache.shutdown()
        stopper.set()

    worker = threading.Thread(target=stop, daemon=True)
    worker.start()
    assert stopper.wait(5), "shutdown should return with work outstanding"
    assert cache.io_loop_running, "shutdown must not stop under outstanding work"
    assert not cache._finalized, "and must not claim to have finalised"

    # The late completion. One owner releases the charge and the finalizer runs
    # once, stopping the loop and the credential executor together.
    release.set()
    deadline = time.monotonic() + 5
    while (cache.io_loop_running or cache._readers is not None) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not cache.io_loop_running, "the late completion must stop the I/O loop"
    assert cache._readers is None, "and the credential executor with it"
    assert cache._finalized

    # And it is the same finalizer, not a second teardown: calling shutdown
    # again is inert rather than a second stop.
    cache.shutdown()
    assert cache._finalized


def test_a_lease_admitted_after_shutdown_is_refused_and_owns_no_threads(store, monkeypatch):
    """B1-shutdown-admission: admission is atomic with the closed transition.

    A waiter that queued before shutdown and is handed a slot afterwards must
    not then register work, build a credential executor and read a credential
    over a pool that has already finished shutting down. The thread names are
    the evidence: a pool that has shut down leaves no kb-vector threads behind.
    """
    monkeypatch.setattr(vector_clients, "IO_JOIN_SECONDS", 0.25)
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built, max_outstanding=1)

    # Take the only slot, so the second lease queues behind it.
    held = threading.Event()
    release = threading.Event()

    def hold():
        with cache.lease(identity):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)
    # This pool's own I/O thread, so the end-state assertion is about this pool
    # and not about whatever else is running in the same process.
    io_thread = cache._io._thread
    assert io_thread is not None and io_thread.is_alive()

    # The waiter starts before shutdown but cannot have its slot yet.
    result = []
    waiter = threading.Thread(
        target=lambda: _try_lease(cache, identity, Deadline(5.0), result), daemon=True
    )
    waiter.start()

    cache.shutdown()  # the pool is now closed and drained
    release.set()
    holder.join(5)
    waiter.join(5)

    # Wait on the thread object, not on io_loop_running: that property reports
    # the handle being cleared, which stop() does before it joins, so it goes
    # false while the thread is still winding down.
    deadline = time.monotonic() + 5
    while (io_thread.is_alive() or cache._readers is not None) and time.monotonic() < deadline:
        time.sleep(0.005)

    # The late waiter may run, and it may be handed the released slot, but it
    # must not get as far as owning work or spawning anything.
    assert result == ["VectorClientPoolClosed"], result
    assert cache._readers is None, "no credential executor may exist after shutdown"
    assert not cache.io_loop_running
    assert cache._finalized
    assert not io_thread.is_alive(), "the pool's own I/O thread must be gone"


def test_a_late_ordinary_acquisition_failure_still_finalizes(store, monkeypatch):
    """B1-late-shutdown-finalizer, attempt 2: finalise after the decrement.

    An acquisition registered before shutdown can fail after the bounded wait
    expired. The finalizer used to run while it was still charged, decide the
    pool was not drained, and then have nothing left to call it again -- so the
    last work item to finish left the loop and the credential executor running
    until someone called shutdown a second time.
    """
    monkeypatch.setattr(vector_clients, "IO_JOIN_SECONDS", 0.05)
    read_root, identities = store
    identity = identities[0]
    built = []
    cache = pool_for(read_root, built)

    # Warm it, so there is a loop and a credential executor to be left behind.
    with cache.lease(identity) as leased:
        leased.find("q", target_uri="x", limit=1)

    entered, release = threading.Event(), threading.Event()

    def failing_read(_identity):
        entered.set()
        release.wait(5)
        raise RuntimeError("synthetic credential read failure")

    cache.read_key = failing_read
    result = []
    worker = threading.Thread(
        target=lambda: _try_lease(cache, identity, Deadline(0.05), result), daemon=True
    )
    worker.start()
    assert entered.wait(5)

    cache.shutdown()  # the bounded wait expires with the read still open
    assert cache.io_loop_running, "shutdown must not stop under outstanding work"
    assert not cache._finalized

    release.set()
    worker.join(5)

    # The read failed, so nothing was retained: the pool must have finalised
    # itself, with no second shutdown required.
    deadline = time.monotonic() + 5
    while (cache.io_loop_running or cache._readers is not None) and time.monotonic() < deadline:
        time.sleep(0.005)
    # Whether the lease sees the read's error or its own timeout is a real race
    # between the two; what this cause is about is that the pool finalises
    # itself either way, with no second shutdown called for.
    assert result != ["admitted"], result
    assert not cache.io_loop_running, "the last work item must finalise the pool"
    assert cache._readers is None, "and the credential executor with it"
    assert cache._finalized


def test_post_call_release_does_not_wait_behind_another_keys_initialize(store):
    """B1-postcall-cleanup-deadline: finishing the call is the end of the budget.

    The release of a holder is bookkeeping, and it needs the pool lock -- which
    another key's long initialization is holding. A request that has already
    done its work must not sit behind that.
    """
    read_root, identities = store
    first, second = identities[0], identities[1]
    built = []
    entered, release_init = threading.Event(), threading.Event()

    class SlowInit(FakeClient):
        async def initialize(self):
            entered.set()
            await asyncio.to_thread(release_init.wait, 5)

    def make(**kwargs):
        client = SlowInit(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, make)
    # A lease for another key, whose initialize holds the pool lock.
    holder = threading.Thread(
        target=lambda: _lease_and_find(cache, second, Deadline(10.0)), daemon=True
    )
    holder.start()
    assert entered.wait(5), "the other key's initialize should be running"

    # This request has a 30ms budget and its own work is instant.
    began = time.monotonic()
    with pytest.raises(TimeoutError):
        with cache.lease(first, Deadline(0.03)) as leased:
            leased.find("q", target_uri="x", limit=1)
    elapsed = time.monotonic() - began

    assert elapsed < 0.2, f"a finished request waited {elapsed:.3f}s for someone else"
    assert cache._outstanding_work() >= 1, "the deferred release is still charged"

    release_init.set()
    holder.join(5)
    deadline = time.monotonic() + 5
    while cache._outstanding_work() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cache._outstanding_work() == 0, "the deferred release hands the charge back"
    cache.shutdown()


def _lease_and_find(cache, identity, budget, _outcome=None):
    _outcome = _outcome if _outcome is not None else []
    try:
        with cache.lease(identity, budget) as leased:
            leased.find("q", target_uri="x", limit=1)
    except Exception as exc:  # this thread only exists to hold the lock
        _outcome.append(type(exc).__name__)


def test_a_cancelled_call_returns_at_its_budget_not_budget_plus_grace(store):
    """B1-request-cancel-grace: the grace is not the caller's to pay.

    A budget of twenty milliseconds with a cancel grace of two hundred returned
    at two hundred and twenty, for a find and for an initialize alike. The
    submission is handed to the pool instead, and the caller goes straight
    back; ownership, not waiting, is what keeps the work safe.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    started, release = threading.Event(), threading.Event()

    class Deaf(FakeClient):
        async def find(self, query, **kwargs):
            started.set()
            try:
                await asyncio.to_thread(release.wait, 5)
            except asyncio.CancelledError:
                # Observes the cancellation and refuses to unwind, which is the
                # case the grace used to be spent on.
                self.cancelled = True
                await asyncio.to_thread(release.wait, 5)
                raise
            return {"resources": []}

    def make(**kwargs):
        client = Deaf(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, make, cancel_grace=0.2)
    t0 = time.monotonic()
    with pytest.raises(IndexCallStillOpen) as raised:
        with cache.lease(identity, Deadline(0.02)) as leased:
            leased.find("q", target_uri="x", limit=1)
    elapsed = time.monotonic() - t0

    assert "did not stop" in str(raised.value)
    assert elapsed < 0.15, f"budget plus grace was paid: {elapsed:.3f}s"
    # The work is still owned, not forgotten: the charge is held until the real
    # unwind, and the finalizer cannot have run.
    assert cache._outstanding_work() >= 1
    assert not cache._finalized

    release.set()
    # The pool is live, so nothing is torn down until it is shut down. What has
    # to be finished first is the charge, which the real unwind releases.
    deadline = time.monotonic() + 5
    while cache._outstanding_work() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cache._outstanding_work() == 0
    cache.shutdown()
    assert wait_idle(cache)


def test_a_cancelled_initialize_returns_at_its_budget_not_budget_plus_grace(store):
    """The same for initialization, which is the other call that can hang."""
    read_root, identities = store
    identity = identities[0]
    built = []
    release = threading.Event()
    began = [0.0]

    class DeafInit(FakeClient):
        async def initialize(self):
            began[0] = time.monotonic()
            try:
                await asyncio.to_thread(release.wait, 5)
            except asyncio.CancelledError:
                self.cancelled = True
                await asyncio.to_thread(release.wait, 5)
                raise

    def make(**kwargs):
        client = DeafInit(**kwargs)
        built.append(client)
        return client

    cache = VectorClientCache(URL, read_root, make, cancel_grace=0.2)
    with pytest.raises(TimeoutError):
        with cache.lease(identity, Deadline(0.02)) as leased:
            leased.find("q", target_uri="x", limit=1)
    elapsed = time.monotonic() - began[0]

    assert elapsed < 0.15, f"budget plus grace was paid: {elapsed:.3f}s"
    assert wait_flag(built[0], "cancelled"), "the cancellation must reach the SDK coroutine"
    release.set()
    # The pool is live, so nothing is torn down until it is shut down. What has
    # to be finished first is the charge, which the real unwind releases.
    deadline = time.monotonic() + 5
    while cache._outstanding_work() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert cache._outstanding_work() == 0
    cache.shutdown()
    assert wait_idle(cache)


def test_detached_closes_cannot_grow_past_the_owned_client_cap(store):
    """B1-detached-close-capacity: the cap counts clients that exist.

    Forty rotations with a pool of one and one outstanding call left forty
    unclosed clients and thirty-nine background closes, because the bound
    counted only what the pool still held. Capacity is now taken before a
    client is built and held until its close lands, so with the closes held open
    the pool runs out of capacity and says so rather than building a forty-first
    SDK client it has no room for.
    """
    read_root, identities = store
    identity = identities[0]
    built = []
    revision = [KEY]
    release_close = threading.Event()

    class HeldClose(FakeClient):
        async def close(self):
            await asyncio.to_thread(release_close.wait, 5)
            self.closed = True
            self.closes += 1

    cache = VectorClientCache(
        URL,
        read_root,
        lambda **kw: _record(built, HeldClose(**kw)),
        max_clients=1,
        max_outstanding=1,
        cancel_grace=0.05,
    )
    cache.read_key = lambda _identity: (revision[0], credential_revision(revision[0]))

    admitted = 0
    for step in range(40):
        revision[0] = f"synthetic-read-key-{step}"
        try:
            with cache.lease(identity, Deadline(0.05)) as leased:
                leased.find("q", target_uri="x", limit=1)
            admitted += 1
        except TimeoutError:
            pass
        # The cap is on clients that exist, not on what the pool still holds, so
        # this holds however many rotations are attempted and however long the
        # closes are held open.
        assert len(built) <= 1, f"step {step}: built {len(built)} clients"

    assert admitted < 40, "a one-client cap must refuse some of forty rotations"
    assert len(built) == 1

    release_close.set()
    cache.shutdown()
    assert wait_idle(cache)
    assert wait_closed(built)


def _record(built, client):
    built.append(client)
    return client


def _live_clients(built):
    return sum(1 for c in built if not c.closed)


def _record(built, client):
    built.append(client)
    return client


def _live_clients(built):
    return sum(1 for c in built if not c.closed)


def test_a_failing_release_step_still_hands_the_charge_back(store):
    """A release step that raises must not strand the pool.

    This is the shape of the run that had to be stopped. The charge was
    decremented after the release step, so an exception there left the pool
    permanently undrainable and every teardown in the rest of the run waited
    out its full timeout instead of failing.
    """
    read_root, _identities = store
    built = []
    cache = pool_for(read_root, built)

    def failing_release():
        raise ValueError("synthetic release failure")

    cache._own_work("release-failure", lambda: None, failing_release)
    deadline = time.monotonic() + 5
    while cache._outstanding_work() and time.monotonic() < deadline:
        time.sleep(0.005)

    assert cache._outstanding_work() == 0, "the charge is handed back regardless"
    cache.shutdown()
    assert wait_idle(cache)


def test_client_built_after_budget_expiry_keeps_capacity_until_close_finishes(store):
    """A blocked close keeps the one slot reserved for client construction."""
    read_root, identities = store
    identity = identities[0]
    built = []
    close_started = threading.Event()
    release_close = threading.Event()
    WAIT_TIMEOUT = 2.0
    CAPACITY_BUDGET = 0.05
    now = [0.0]

    def make(**kwargs):
        client = FakeClient(**kwargs)
        built.append(client)

        async def close():
            close_started.set()
            while not release_close.is_set():
                try:
                    await asyncio.sleep(0.001)
                except asyncio.CancelledError:
                    continue
            client.closed = True
            client.closes += 1

        client.close = close
        now[0] = 2.0
        return client

    cache = VectorClientCache(
        URL,
        read_root,
        make,
        max_clients=1,
        max_outstanding=1,
        clock=lambda: now[0],
    )
    try:
        gone = Deadline(1.0, clock=lambda: now[0])
        with pytest.raises(TimeoutError):
            with cache.lease(identity, gone):
                pass

        assert len(built) == 1
        assert wait_event(close_started, WAIT_TIMEOUT), "the constructed client was not closed"

        with pytest.raises(TimeoutError):
            with cache.lease(identity, Deadline(CAPACITY_BUDGET)):
                pass

        assert len(built) == 1, "capacity was reused while the first close was blocked"
    finally:
        release_close.set()
        cache.shutdown()


def test_failed_initialization_keeps_capacity_until_blocked_close_finishes(store):
    """A failed initialize cannot return capacity while its close is running."""
    read_root, identities = store
    identity = identities[0]
    built = []
    close_started = threading.Event()
    release_close = threading.Event()
    WAIT_TIMEOUT = 2.0
    CAPACITY_BUDGET = 0.05

    def make(**kwargs):
        client = FakeClient(**kwargs)
        built.append(client)

        async def initialize():
            raise RuntimeError("initialize failed")

        async def close():
            close_started.set()
            while not release_close.is_set():
                try:
                    await asyncio.sleep(0.001)
                except asyncio.CancelledError:
                    continue
            client.closed = True
            client.closes += 1

        client.initialize = initialize
        client.close = close
        return client

    cache = VectorClientCache(
        URL,
        read_root,
        make,
        max_clients=1,
        max_outstanding=1,
    )
    try:
        with pytest.raises(RuntimeError, match="initialize failed"):
            with cache.lease(identity, Deadline(1.0)):
                pass

        assert len(built) == 1
        assert wait_event(close_started, WAIT_TIMEOUT), "the failed client was not closed"

        with pytest.raises(TimeoutError):
            with cache.lease(identity, Deadline(CAPACITY_BUDGET)):
                pass

        assert len(built) == 1, "failed initialization returned capacity before close"
    finally:
        release_close.set()
        cache.shutdown()


def test_expired_optimistic_cache_hit_cannot_build_without_capacity(
    store, monkeypatch
):
    read_root, identities = store
    built = []
    now = [0.0]
    armed = [False]

    def clock():
        value = now[0]
        if armed[0]:
            armed[0] = False
            now[0] = 2.0
        return value

    cache = pool_for(
        read_root,
        built,
        ttl_seconds=1.0,
        max_clients=1,
        max_outstanding=1,
        clock=clock,
    )
    identity = identities[0]
    close_started = threading.Event()
    release_close = threading.Event()
    result = []
    try:
        with cache.lease(identity):
            pass
        assert len(built) == 1
        first = built[0]
        original_close = first.close

        async def blocked_close():
            close_started.set()
            if not release_close.wait(5.0):
                raise AssertionError("blocked close was not released")
            await original_close()

        monkeypatch.setattr(first, "close", blocked_close)
        armed[0] = True
        _try_lease(cache, identity, None, result)
        assert wait_event(close_started, timeout=5.0)
        assert len(built) == 1
        assert result == ["timeout"]
    finally:
        release_close.set()
        cache.shutdown()
    assert wait_closed(built)
