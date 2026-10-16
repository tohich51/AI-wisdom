"""Bounded, shared OpenViking SDK clients for read-only retrieval.

Why this file exists, and what was *read* in the installed SDK rather than
assumed (openviking_sdk in the shared venv, at the accepted F, 2026-10-16):

* ``AsyncHTTPClient`` is the SDK's public async surface -- it is in the
  package's ``__all__`` -- and ``initialize()`` builds exactly one
  ``httpx.AsyncClient`` behind it. Reusing that client is what the pool buys;
  constructing one per query -- the shape the accepted F has -- is what it
  does not. A search fans out over up to twenty libraries, so the per-query
  shape paid twenty TLS handshakes and twenty ``initialize()`` calls before
  the first result could come back.

* ``find()`` takes no timeout. The socket timeout is fixed when the client is
  constructed (``httpx.AsyncClient(timeout=self._timeout)``), so a client
  shared between two requests cannot carry either request's deadline. That is
  the defect this module exists to answer, and the answer is deliberately
  *not* a shorter socket timeout: a client cached by an earlier caller under
  a large budget would hand that budget to every later caller, which is
  exactly the case the contract forbids. A pooled client's timeout here is
  therefore a fixed network ceiling that no request's budget can shape, and
  the per-request bound is enforced where a per-request value can actually
  exist -- around the call.

* The bound is a real one. Every SDK coroutine runs on one event loop owned
  by this module (:class:`_IODispatcher`); the caller waits for its own
  remaining budget and, on expiry, *cancels* the call. Cancelling the
  submitted task raises ``CancelledError`` inside httpx, which tears the
  connection down rather than leaving a request on the wire. Measured
  against a real socket that accepts a request and never answers: the caller
  returns at its own budget rather than at the client's socket timeout, the
  SDK coroutine receives the cancellation and unwinds, httpx's pool drops the
  connection, and the peer observes the connection break. A future that was
  merely no longer waited on would satisfy none of that while looking
  identical from the call site, so the two are kept apart on purpose: an
  expired budget that could not be unwound is reported as its own failure
  (:class:`IndexCallStillOpen`) instead of being dressed up as a timeout.

* ``SyncHTTPClient`` is not used here, and the reason is the same property.
  It reaches the identical async client through ``run_async()``, which
  dispatches onto one process-wide worker loop in a daemon thread and blocks
  the calling thread on ``future.result()`` with no handle to interrupt it.
  A caller that stops waiting there has cancelled nothing, and the loop
  belongs to the whole process, so this module could not shut it down even if
  it wanted to.

* ``run_async()`` also means the sync path gives no network parallelism of its
  own. On the loop below, calls really do overlap, so ``MAX_OUTSTANDING``
  bounds genuine in-flight requests rather than queue depth.

The key is ``(endpoint, account, profile, credential revision)``. The raw
secret is never a key and never reaches a log: ``credential_revision`` is a
truncated digest, held only in this process, and the entry holds the SDK
client that already carries the secret. Rotation is therefore a key change,
and the old entry is evicted and closed on its own.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import hashlib
import inspect
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from openviking_sdk import AsyncHTTPClient

from kb.adapters.secrets import read_credentials
from kb.retrieval.embedding_profile import SELECTED_PROFILE

#: A network ceiling for one SDK call, in seconds, and the timeout every
#: pooled client is built with. It is a property of the *client*, fixed here,
#: and is never a request's remaining budget: baking a request's budget into
#: a shared client is how a later, shorter request ends up waiting on an
#: earlier, longer one. Requests are bounded by the deadline instead.
NETWORK_TIMEOUT = 45.0

#: How long a cancelled call may take to actually unwind before the pool
#: admits that the I/O is still open. Generous, because the normal case is a
#: socket close that completes in microseconds, and finite, because a caller
#: waiting forever for teardown is the same bug in a different costume.
CANCEL_GRACE_SECONDS = 5.0

#: How long closing one client may take before it is reported as failed. A
#: shutdown that hangs is not a shutdown, and a close that has not finished
#: in this long is not going to.
CLOSE_TIMEOUT_SECONDS = 5.0

#: How long the I/O thread is given to notice the stop request and finish.
IO_JOIN_SECONDS = 5.0

#: Bounded pools, not a cache framework. The numbers come from a short
#: measurement recorded in the B1 receipt, not from a framework default.
#:
#: A semantic search fans out over up to ``LIBRARY_BUDGET`` (20) libraries, and
#: each library has its own index account, so a full search touches 20 distinct
#: read identities. That is the working set the bound has to cover. Measured on
#: a fixed 25 x 20 workload, a bound of 8 rebuilt a client for all 500 queries
#: -- no reuse at all, because every pass evicted the entry the next pass
#: wanted. At 20 or more the same workload built no clients after the first
#: pass. Thirty-two covers the working set with headroom and still caps the
#: socket count however many index accounts exist. A bound under the working
#: set is worse than no pool: it pays eviction on top of the handshake.
MAX_CLIENTS = 32
CLIENT_TTL_SECONDS = 300.0

#: Concurrent in-flight SDK calls. One request fans out over the same twenty
#: identities, so the bound is set the same way and for the same reason: high
#: enough not to serialise a single search against itself, low enough that the
#: pool's ceiling is not decorative.
MAX_OUTSTANDING = 20

#: One sanitised spelling of the expired-budget failure, so the caller and the
#: pool never drift apart on what a timeout is called.
INDEX_DEADLINE_EXCEEDED = "index_deadline_exceeded"

ClientKey = tuple[str, str, str, str]

#: Close failures are reported by type only. A close that fails can carry the
#: endpoint and, in some transports, the credential in its message, so the
#: message is never the thing that gets written down.
_logger = logging.getLogger(__name__)


class IndexCallStillOpen(RuntimeError):
    """The budget expired, the call was cancelled, and the I/O did not stop.

    Distinct from ``TimeoutError`` on purpose. A deadline that was met and a
    deadline after which the request was genuinely torn down are different
    facts, and a caller that cleans up differently for the two must be able to
    tell them apart. This one says the cancellation did not take within the
    grace period, so the request may still be on the wire.
    """


class VectorClientPoolClosed(RuntimeError):
    """A client was asked for after the pool had already been shut down."""


def credential_revision(secret: str) -> str:
    """A short, in-process fingerprint of a credential.

    Not a log key and not a secret: a truncated digest, used only to tell one
    credential from another so rotation evicts instead of reusing. Two reads
    of the same key produce the same revision; a rewritten key does not.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be non-empty")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


class Deadline:
    """One budget, shared by the queue wait and the network call.

    ``remaining`` is what is left of it. A caller that cannot get a client
    before the budget is gone must not start a call it cannot wait for, and a
    caller whose budget is already gone must not start one at all.
    """

    __slots__ = ("_clock", "_expiry")

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise ValueError("deadline must be positive")
        self._clock = clock
        self._expiry = clock() + float(seconds)

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self._expiry - self._clock())

    def timeout_for(self, ceiling: float = NETWORK_TIMEOUT) -> float:
        """The smaller of a ceiling and what is left of the budget."""
        return max(0.0, min(float(ceiling), self.remaining))


class _Submission:
    """One coroutine handed to the I/O loop, and what became of it.

    ``finished`` is the only thing that proves the coroutine is no longer
    running. The ``concurrent.futures`` future goes to CANCELLED the instant
    ``cancel()`` is called, before the task has seen anything, so waiting on
    that future would confirm nothing; waiting on this event is what turns
    "we stopped waiting" into "the request actually stopped".
    """

    __slots__ = ("cancelled", "finished")

    def __init__(self) -> None:
        self.finished = threading.Event()
        self.cancelled = False

    async def run(self, make_coro: Callable[[], Any]) -> Any:
        try:
            produced = make_coro()
            # The SDK's client methods are coroutines. Accepting a plain return
            # keeps a synchronous double usable here rather than turning a
            # working test into a TypeError about awaiting None.
            if inspect.isawaitable(produced):
                return await produced
            return produced
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    def await_unwound(self, grace: float) -> bool:
        return self.finished.wait(grace)


class _IODispatcher:
    """One event loop, in one thread, for every SDK client a pool owns.

    An ``httpx.AsyncClient`` belongs to the loop that drives it, so the
    clients are built, used and closed here and nowhere else. The loop is
    started on first use and stopped by :meth:`stop`, which is what makes
    shutdown a fact rather than a hope: once ``stop`` has joined the thread,
    no coroutine this pool submitted is still running and no socket it opened
    is still open.
    """

    def __init__(
        self,
        grace: float = CANCEL_GRACE_SECONDS,
        join_timeout: float = IO_JOIN_SECONDS,
    ) -> None:
        self._grace = float(grace)
        self._join_timeout = float(join_timeout)
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._stopped:
                raise VectorClientPoolClosed("vector client io loop is stopped")
            if self._thread is not None and self._thread.is_alive():
                assert self._loop is not None
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._serve,
                args=(loop,),
                name="kb-vector-io",
                daemon=True,
            )
            self._loop = loop
            self._thread = thread
            thread.start()
            return loop

    @staticmethod
    def _serve(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as exc:  # pragma: no cover - interpreter teardown
                _logger.debug("vector io loop drain failed: %s", type(exc).__name__)
            loop.close()

    def run(self, make_coro: Callable[[], Any], budget: float | None = None) -> Any:
        """Run one coroutine on the loop, bounded by ``budget`` seconds.

        The bound is enforced by cancelling the task, not by giving up on it.
        On expiry the task is cancelled -- ``CancelledError`` is raised inside
        httpx and the connection is closed -- and the call only returns once
        the task has actually finished. If it has not, that is reported as
        :class:`IndexCallStillOpen` rather than as a timeout, because the
        request may still be on the wire.
        """
        if budget is not None and budget <= 0:
            # No budget, no call. Nothing is built and nothing is sent, which
            # is both the cheaper answer and the truthful one: there is no
            # request here to report as still open, and a task cancelled
            # before its first step never runs a line of its own body.
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)

        loop = self._ensure()
        submission = _Submission()
        future: concurrent.futures.Future = concurrent.futures.Future()
        started: dict[str, asyncio.Task] = {}

        def start() -> None:
            task = loop.create_task(submission.run(make_coro))
            started["task"] = task
            task.add_done_callback(settle)

        def settle(task: asyncio.Task) -> None:
            # ``finished`` is set from the task, not from inside the
            # coroutine. A task cancelled before its first step never executes
            # its body, so a ``finally`` in there would not fire, and "the call
            # unwound" would be a claim nothing backs.
            submission.finished.set()
            if future.cancelled():
                return
            if task.cancelled():
                future.cancel()
                return
            error = task.exception()
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(task.result())

        def request_cancel() -> None:
            task = started.get("task")
            if task is not None and not task.done():
                task.cancel()

        try:
            # ``start`` is queued first, so ``request_cancel`` -- which may be
            # queued microseconds later, from another thread -- always finds a
            # task to cancel. The cancellation is delivered on the loop, which
            # is the only place a task can be cancelled from; asking the
            # concurrent future would only mark the future, and the request
            # would sit on the wire until the client's own socket timeout.
            loop.call_soon_threadsafe(start)
        except RuntimeError:  # pragma: no cover - the loop stopped under us
            raise VectorClientPoolClosed("vector client io loop is stopped") from None

        try:
            return future.result(timeout=budget)
        except concurrent.futures.TimeoutError:
            return self._abandon(loop, request_cancel, future, submission)

    def _abandon(
        self,
        loop: asyncio.AbstractEventLoop,
        request_cancel: Callable[[], None],
        future: Any,
        submission: _Submission,
    ) -> Any:
        future.cancel()
        try:
            loop.call_soon_threadsafe(request_cancel)
        except RuntimeError:  # pragma: no cover - the loop stopped under us
            pass
        if not submission.await_unwound(self._grace):
            raise IndexCallStillOpen("index call did not stop after cancellation")
        if not submission.cancelled:
            # It stopped, but not because we cancelled it: either it had not
            # begun when the cancel landed, or the socket timeout or the
            # provider beat us to it. The budget was still missed, so this is
            # a timeout -- but the provider-side work is not being claimed as
            # cancelled on the strength of our ask.
            _logger.debug("vector call ended without observing cancellation")
        raise TimeoutError(INDEX_DEADLINE_EXCEEDED)

    def stop(self) -> None:
        """Stop the loop and wait for its thread. Idempotent."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
            self._stopped = True
        if loop is None or thread is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:  # pragma: no cover - already stopped
            pass
        thread.join(timeout=self._join_timeout)
        if thread.is_alive():  # pragma: no cover - the loop refused to stop
            _logger.warning("vector io thread did not stop within the join timeout")


@dataclass
class _Entry:
    key: ClientKey
    client: Any
    last_used: float
    holders: int = 0
    retired: bool = False

    def touch(self, now: float) -> None:
        self.last_used = now


class LeasedClient:
    """A pooled client, reachable only through this call's own budget.

    The raw SDK client is deliberately not handed out. Every provider call
    goes through :meth:`call`, so no request can reach the network outside
    the budget it was given, whatever socket timeout the client it borrowed
    happened to be built with. The client is not the caller's to close; the
    pool closes it when eviction, rotation or shutdown says so.
    """

    __slots__ = ("_client", "_deadline", "_pool", "revision")

    def __init__(
        self,
        pool: VectorClientCache,
        client: Any,
        revision: str,
        deadline: Deadline | None,
    ) -> None:
        self._pool = pool
        self._client = client
        self.revision = revision
        self._deadline = deadline

    @property
    def deadline(self) -> Deadline | None:
        return self._deadline

    def call(self, make_coro: Callable[[], Any]) -> Any:
        """Run one SDK coroutine, bounded by this lease's remaining budget."""
        remaining = self._deadline.remaining if self._deadline is not None else None
        return self._pool._io.run(make_coro, remaining)

    def find(self, query: str, **kwargs: Any) -> Any:
        """One bounded ``find()`` against the pooled client."""
        return self.call(lambda: self._client.find(query, **kwargs))


class VectorClientCache:
    """A bounded, thread-safe pool of SDK clients with explicit lifecycle.

    Construction, eviction, rotation and shutdown all close the clients they
    drop. A client is only ever closed once its last holder has released it,
    so no close can land on a call that is still running on the I/O loop, and
    the loop itself is stopped by :meth:`shutdown` rather than at process
    exit.
    """

    def __init__(
        self,
        url: str,
        read_root,
        client_factory: Callable[..., Any] = AsyncHTTPClient,
        *,
        max_clients: int = MAX_CLIENTS,
        ttl_seconds: float = CLIENT_TTL_SECONDS,
        max_outstanding: int = MAX_OUTSTANDING,
        network_timeout: float = NETWORK_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
        cancel_grace: float = CANCEL_GRACE_SECONDS,
    ) -> None:
        self._url = url
        self._read_root = read_root
        self._factory = client_factory
        self._max_clients = max(1, int(max_clients))
        self._ttl = float(ttl_seconds)
        self._network_timeout = float(network_timeout)
        self._clock = clock
        self._entries: dict[ClientKey, _Entry] = {}
        self._lock = threading.RLock()
        # Holder ownership must outlive _entries.clear() during shutdown.
        self._holders_changed = threading.Condition(self._lock)
        self._live_holders: dict[int, int] = {}
        self._closed = False
        self._io = _IODispatcher(grace=cancel_grace)
        # A last resort for a process that exits without an explicit shutdown,
        # so httpx connection pools and sockets are not left to the
        # interpreter's reclaimer. Idempotent, and never the primary path:
        # the gateway's lifespan calls shutdown() directly.
        atexit.register(self.shutdown)
        # Bounds how many SDK calls may be in flight at once.
        self._outstanding = threading.BoundedSemaphore(max(1, int(max_outstanding)))

    # -- observable state --------------------------------------------------

    @property
    def is_shutdown(self) -> bool:
        return self._closed

    @property
    def io_loop_running(self) -> bool:
        return self._io.running

    # -- keys ---------------------------------------------------------------

    def key_for(self, identity: str, revision: str) -> ClientKey:
        return (self._url, identity, SELECTED_PROFILE.profile_hash, revision)

    def read_key(self, identity: str) -> tuple[str, str]:
        """The credential for a read identity, and its revision.

        The file is read per call rather than cached: rotation is then
        observed on the next request instead of after a TTL, and the only
        thing retained between calls is the revision, never the secret.
        """
        secret = read_credentials(self._read_root, identity)
        return secret, credential_revision(secret)

    # -- lifecycle ----------------------------------------------------------

    def _close(self, entry: _Entry) -> None:
        try:
            self._io.run(entry.client.close, CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:
            # A close failure is not a reason to fail a search, and after a
            # shutdown it is not even a reason to restart the loop.
            _logger.debug("vector client close failed: %s", type(exc).__name__)

    def _retire_locked(self, entry: _Entry) -> None:
        if entry.holders:
            # A live call owns this client; closing now would land on it.
            entry.retired = True
            return
        if self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key, None)
        self._close(entry)

    def _evict_locked(self, now: float) -> None:
        for entry in [e for e in self._entries.values() if e.holders == 0 and self._stale(e, now)]:
            if self._entries.get(entry.key) is entry:
                self._entries.pop(entry.key, None)
            self._close(entry)
        while len(self._entries) > self._max_clients:
            idle = [e for e in self._entries.values() if e.holders == 0]
            if not idle:
                # Everything is in use. Growing past the bound would be a
                # silent leak, so the extra keys wait for a release instead.
                return
            victim = min(idle, key=lambda e: e.last_used)
            self._entries.pop(victim.key, None)
            self._close(victim)

    def _stale(self, entry: _Entry, now: float) -> bool:
        return self._ttl <= 0 or (now - entry.last_used) > self._ttl

    def _acquire_slot(self, deadline: Deadline | None) -> bool:
        """Wait for an outstanding slot, bounded by the shared budget."""
        if deadline is None:
            self._outstanding.acquire()
            return True
        remaining = deadline.remaining
        return self._outstanding.acquire(timeout=remaining) if remaining > 0 else False

    def _build_locked(self, key: ClientKey, secret: str) -> _Entry:
        # The timeout here is the fixed network ceiling and nothing else. It
        # does not consult the caller's budget, because a client is shared: a
        # timeout derived from one request's deadline is inherited by every
        # later request that reuses the client.
        client = self._factory(url=key[0], api_key=secret, timeout=self._network_timeout)
        self._io.run(client.initialize, CLOSE_TIMEOUT_SECONDS)
        return _Entry(key=key, client=client, last_used=self._clock())

    @contextmanager
    def lease(self, identity: str, deadline: Deadline | None = None) -> Iterator[LeasedClient]:
        """One client, reference counted, for the duration of one call.

        Yields a :class:`LeasedClient`: the pooled client and the credential
        revision it was built for, reachable only through this call's budget.
        The caller must not close it; releasing is the pool's business, and
        the pool closes it when eviction, rotation or shutdown says so.
        """
        secret, revision = self.read_key(identity)
        key = self.key_for(identity, revision)

        if self._closed:
            raise VectorClientPoolClosed("vector client pool is shut down")

        if not self._acquire_slot(deadline):
            # The budget was gone before the call started, so nothing was
            # sent. This is a timeout, not a cancelled request.
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)

        entry: _Entry | None = None
        holder_owner = threading.get_ident()
        stop_io = False
        try:
            with self._lock:
                if self._closed:
                    raise VectorClientPoolClosed("vector client pool is shut down")
                now = self._clock()
                # Rotation. A different revision for the same identity means
                # the credential that key was built with is gone, so its client
                # is closed now. Waiting for the TTL to sweep it would keep a
                # retired credential's connection alive for minutes after the
                # rotation that replaced it.
                for stale in [
                    e
                    for e in self._entries.values()
                    if e.key != key and e.key[0] == key[0] and e.key[1] == key[1]
                ]:
                    self._retire_locked(stale)
                existing = self._entries.get(key)
                if existing is not None and not existing.retired and not self._stale(existing, now):
                    existing.touch(now)
                    entry = existing
                else:
                    if existing is not None:
                        # Idle past its TTL: replace it rather than hand back a
                        # connection that has been sitting since.
                        self._retire_locked(existing)
                    if deadline is not None and deadline.expired:
                        raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
                    # Built under the lock so two callers cannot both build a
                    # client for one key and leave one of them unowned.
                    entry = self._build_locked(key, secret)
                    self._entries[key] = entry
                # Counted before eviction so the entry this call is about to
                # use is not the one eviction drops.
                entry.holders += 1
                self._live_holders[holder_owner] = self._live_holders.get(holder_owner, 0) + 1
                self._evict_locked(now)

            try:
                yield LeasedClient(self, entry.client, revision, deadline)
            finally:
                with self._lock:
                    entry.holders -= 1
                    if entry.holders == 0:
                        if entry.retired or self._stale(entry, self._clock()):
                            # Guarded by identity: a rotated-out entry shares its
                            # key with the replacement and must not evict it.
                            if self._entries.get(entry.key) is entry:
                                self._entries.pop(entry.key, None)
                            self._close(entry)
                        else:
                            entry.touch(self._clock())
                    # Releasing is when a client becomes idle, and it is the
                    # only moment the bound can be restored without waiting for
                    # the next acquire. Without this, concurrent callers that
                    # pushed the pool past max_clients leave it over the bound
                    # until traffic picks up again.
                    self._evict_locked(self._clock())
                    # Keep ownership registered until all required closes finish.
                    self._live_holders[holder_owner] -= 1
                    if self._live_holders[holder_owner] == 0:
                        del self._live_holders[holder_owner]
                        self._holders_changed.notify_all()
                    stop_io = self._closed and not self._live_holders
        finally:
            if stop_io:
                self._io.stop()
            self._outstanding.release()

    def shutdown(self) -> None:
        """Retire every client and stop the I/O loop when teardown is safe.

        Clients still in use are closed by the holder that releases them last.
        Shutdown waits up to ``IO_JOIN_SECONDS`` for holders owned by other
        threads; a holder calling shutdown in its own thread is reported
        immediately instead of waiting on itself. If holders remain after the
        bounded wait, shutdown reports the still-open work and leaves the loop
        running. The last release then closes the retired client and stops the
        loop, so shutdown never closes or stops it under a live call.
        """
        with self._lock:
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                if entry.holders:
                    entry.retired = True
                else:
                    self._close(entry)
            same_thread = threading.get_ident() in self._live_holders
            still_open = sum(self._live_holders.values())
            if still_open and not same_thread:
                self._holders_changed.wait_for(
                    lambda: not self._live_holders,
                    timeout=IO_JOIN_SECONDS,
                )
                still_open = sum(self._live_holders.values())
            if still_open:
                _logger.warning(
                    "vector client shutdown incomplete: %d holder(s) still open; "
                    "I/O loop remains running until they release",
                    still_open,
                )
        if not still_open:
            self._io.stop()

    # ``close`` is the spelling the rest of the codebase uses for an owned
    # resource; keep both so a shutdown hook does not have to know which.
    close = shutdown

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        # Endpoint and counts only. A key holds a credential revision, and a
        # repr is a log line.
        return f"VectorClientCache(endpoint={self._url!r}, clients={len(self)})"
