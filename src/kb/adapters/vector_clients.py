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

#: Worker threads for the interruptible credential read. A blocking read that
#: must be bounded cannot run on the caller's own thread, and a thread per
#: read would trade a deadline for thread churn, so this is a small reused
#: pool sized to the outstanding bound, which is how many reads can be in
#: flight at once anyway.
CREDENTIAL_WORKERS = MAX_OUTSTANDING

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

    ``submission`` is the exact unfinished submission whose ``finished`` event
    proves when the client may finally be retired and released.
    """

    def __init__(self, message: str, submission: _Submission) -> None:
        super().__init__(message)
        self.submission = submission


class _ReadOverran(Exception):
    """Internal: a credential read outlived the caller's budget.

    Carries the worker future so the caller can hand this acquisition's slot
    and its pool registration to whoever waits for the read to actually end.
    It never escapes this module: :meth:`VectorClientCache.lease` turns it into
    the ordinary ``TimeoutError`` a caller already knows.
    """

    def __init__(self, future: concurrent.futures.Future) -> None:
        super().__init__("credential read outlived the deadline")
        self.future = future


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

    @property
    def in_io_thread(self) -> bool:
        """Whether the caller is the loop thread itself.

        Blocking on the loop from the loop is a deadlock that reports nothing:
        the wait can only end when the thing being waited for runs, and the
        thing being waited for is the caller. Both entry points below refuse
        it rather than hanging.
        """
        return threading.current_thread() is self._thread

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
        if self.in_io_thread:
            raise RuntimeError("vector client I/O cannot be awaited from the I/O loop thread")
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
            raise IndexCallStillOpen(
                "index call did not stop after cancellation",
                submission=submission,
            )
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
            if self.in_io_thread:
                # Joining this thread from itself never returns.
                _logger.warning("vector io stop requested from the I/O loop thread; ignored")
                return
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
    #: Set the moment this entry is handed to a close. Closes now run outside
    #: the lock, so two selection paths can reach the same entry; without this
    #: a client could be closed twice.
    closed: bool = False

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

    __slots__ = ("_client", "_deadline", "_entry", "_pool", "_stuck", "revision")

    def __init__(
        self,
        pool: VectorClientCache,
        entry: _Entry,
        revision: str,
        deadline: Deadline | None,
    ) -> None:
        self._pool = pool
        self._client = entry.client
        self._entry = entry
        self.revision = revision
        self._deadline = deadline
        self._stuck: IndexCallStillOpen | None = None

    @property
    def deadline(self) -> Deadline | None:
        return self._deadline

    @property
    def unfinished_submission(self) -> _Submission | None:
        return self._stuck.submission if self._stuck is not None else None

    def call(self, make_coro: Callable[[], Any]) -> Any:
        """Run one SDK coroutine, bounded by this lease's remaining budget."""
        if self._stuck is not None:
            raise self._stuck
        remaining = self._deadline.remaining if self._deadline is not None else None
        try:
            return self._pool._io.run(make_coro, remaining)
        except IndexCallStillOpen as exc:
            self._stuck = exc
            self._pool._quarantine(self._entry)
            raise

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
        # Drain accounting has its own lock, and must stay reachable while
        # somebody else holds the pool lock. Two reasons, both measured: an
        # acquisition has to be able to register itself before it is allowed
        # to touch the pool lock at all, or it spends its budget inside its own
        # bookkeeping; and shutdown has to wait for this counter without the
        # pool lock, or the wait blocks the releases it is waiting for. Lock
        # order is always pool lock then drain lock, never the reverse.
        self._holders_changed = threading.Condition(threading.Lock())
        self._live_holders: dict[int, int] = {}
        # Acquisitions that hold a slot but do not have an entry yet: a slow
        # credential read, a contended lock. Shutdown has to see these, or it
        # declares the pool drained while work it started is still running.
        self._pending = 0
        self._readers: concurrent.futures.ThreadPoolExecutor | None = None
        self._readers_lock = threading.Lock()
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
            # shutdown it is not even a reason to restart the loop. It is
            # logged by type: a close message can carry the endpoint, and in
            # some transports the credential.
            _logger.debug("vector client close failed: %s", type(exc).__name__)

    def _close_all(self, entries: list[_Entry]) -> None:
        """Close a batch of dropped clients. Always called with no lock held."""
        for entry in entries:
            self._close(entry)

    def _drop_locked(self, entry: _Entry, closers: list[_Entry]) -> None:
        """Take an idle entry out of the pool and claim its close exactly once.

        Selecting is cheap and happens under the lock; the close itself is
        appended to ``closers`` and run after the lock is released. That is
        the whole point: a close is a round trip to the I/O loop, and holding
        the pool lock across one let a single slow teardown spend a later
        caller's entire budget before that caller's own work began.
        """
        if entry.closed:
            return
        # Guarded by identity: a rotated-out entry shares its key with the
        # replacement and must never evict or close it.
        if self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key, None)
        if not entry.holders:
            entry.closed = True
            closers.append(entry)

    def _retire_locked(self, entry: _Entry, closers: list[_Entry]) -> None:
        if entry.holders:
            # A live call owns this client; closing now would land on it.
            entry.retired = True
            return
        self._drop_locked(entry, closers)

    def _quarantine(self, entry: _Entry) -> None:
        with self._critical(None) as closers:
            self._retire_locked(entry, closers)

    def _evict_locked(self, now: float, closers: list[_Entry]) -> None:
        for entry in [e for e in self._entries.values() if e.holders == 0 and self._stale(e, now)]:
            self._drop_locked(entry, closers)
        while len(self._entries) > self._max_clients:
            idle = [e for e in self._entries.values() if e.holders == 0]
            if not idle:
                # Everything is in use. Growing past the bound would be a
                # silent leak, so the extra keys wait for a release instead.
                return
            victim = min(idle, key=lambda e: e.last_used)
            self._drop_locked(victim, closers)

    def _stale(self, entry: _Entry, now: float) -> bool:
        return self._ttl <= 0 or (now - entry.last_used) > self._ttl

    def _acquire_slot(self, deadline: Deadline | None) -> bool:
        """Wait for an outstanding slot, bounded by the shared budget."""
        if deadline is None:
            self._outstanding.acquire()
            return True
        remaining = deadline.remaining
        return self._outstanding.acquire(timeout=remaining) if remaining > 0 else False

    def _acquire_pool_lock(self, deadline: Deadline | None) -> bool:
        """Take the pool lock, bounded by the caller's own remaining budget.

        This is the wait the completion directive measured: a lease with
        twenty milliseconds left spent a hundred and fifty of them here, in
        front of a lock another thread was holding. The lock is where every
        caller queues behind every other, so leaving it unbounded left the
        shared deadline covering only the semaphore and nothing after it.
        """
        if deadline is None:
            self._lock.acquire()
            return True
        remaining = deadline.remaining
        if remaining <= 0:
            return False
        return self._lock.acquire(timeout=remaining)

    @contextmanager
    def _critical(self, deadline: Deadline | None) -> Iterator[list[_Entry]]:
        """The pool lock, held for bookkeeping only, never for I/O.

        Yields the list of clients the caller wants closed. The closes run
        after the lock has been released.
        """
        if not self._acquire_pool_lock(deadline):
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
        closers: list[_Entry] = []
        try:
            yield closers
        finally:
            self._lock.release()
            self._close_all(closers)

    def _drained(self) -> bool:
        """Whether nothing this pool started is still outstanding.

        Read without the drain lock. It is a predicate over two counters and
        the only consequence of a torn read is one extra turn of the wait loop
        in :meth:`shutdown`, which is bounded and re-evaluates anyway.
        """
        return not self._live_holders and not self._pending

    def _outstanding_work(self) -> int:
        """Live holders plus acquisitions that still own a slot."""
        return sum(self._live_holders.values()) + self._pending

    def _begin_acquisition(self) -> None:
        """Register work that holds a slot but has no entry yet."""
        with self._holders_changed:
            self._pending += 1

    def _end_acquisition(self) -> None:
        """Unregister it, and wake a shutdown that was waiting for it."""
        with self._holders_changed:
            if self._pending:
                self._pending -= 1
            self._holders_changed.notify_all()

    def _ensure_readers(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._readers_lock:
            if self._readers is None:
                self._readers = concurrent.futures.ThreadPoolExecutor(
                    max_workers=max(1, CREDENTIAL_WORKERS),
                    thread_name_prefix="kb-vector-credential",
                )
            return self._readers

    def _stop_readers(self) -> None:
        with self._readers_lock:
            readers, self._readers = self._readers, None
        if readers is not None:
            # cancel_futures drops reads that have not started. A read already
            # running is a local file read: it ends on its own, and the pool
            # is still holding its slot and its pending registration until it
            # does, which is what shutdown waits for.
            readers.shutdown(wait=False, cancel_futures=True)

    def _read_key_bounded(self, identity: str, deadline: Deadline | None) -> tuple[str, str]:
        """Read the credential without letting a slow file spend the budget.

        A credential read is blocking I/O, so with a budget it runs on a small
        reused worker and the caller waits only for what is left. If the read
        overruns, the caller gets its timeout at its own deadline, the result
        is discarded, and no SDK work starts after expiry -- while the pool
        keeps the worker, its slot and its pending registration owned until
        the read really finishes. A future that stopped being waited on is not
        the same as work that stopped.
        """
        if deadline is None:
            return self.read_key(identity)
        remaining = deadline.remaining
        if remaining <= 0:
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
        future = self._ensure_readers().submit(self.read_key, identity)
        try:
            return future.result(timeout=remaining)
        except concurrent.futures.TimeoutError:
            raise _ReadOverran(future) from None

    def _retain_slot_until_read(self, future: concurrent.futures.Future) -> None:
        """Hold this acquisition's slot and registration until the read ends."""

        def release_when_read_finished() -> None:
            try:
                future.result()
            except Exception as exc:  # pragma: no cover - the read already failed
                # By type only: a credential read's message can carry the path
                # it failed on, and the pool does not repeat what it read.
                _logger.debug("credential read failed: %s", type(exc).__name__)
            self._end_acquisition()
            self._outstanding.release()

        threading.Thread(
            target=release_when_read_finished,
            name="kb-vector-credential-release",
            daemon=True,
        ).start()

    def _build_locked(
        self,
        key: ClientKey,
        secret: str,
        deadline: Deadline | None,
        closers: list[_Entry],
    ) -> _Entry:
        # The timeout here is the fixed network ceiling and nothing else. It
        # does not consult the caller's budget, because a client is shared: a
        # timeout derived from one request's deadline is inherited by every
        # later request that reuses the client.
        client = self._factory(url=key[0], api_key=secret, timeout=self._network_timeout)
        budget = (
            CLOSE_TIMEOUT_SECONDS
            if deadline is None
            else deadline.timeout_for(CLOSE_TIMEOUT_SECONDS)
        )
        if budget <= 0:
            # The budget is gone. Build nothing rather than start work that
            # cannot be waited for.
            closers.append(_Entry(key=key, client=client, last_used=self._clock(), closed=True))
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
        try:
            self._io.run(client.initialize, budget)
        except BaseException:
            # A client whose initialization was cancelled is not a usable
            # client and must not be left open. It is claimed for closing here
            # and closed after the lock is released, with the rest.
            closers.append(_Entry(key=key, client=client, last_used=self._clock(), closed=True))
            raise
        return _Entry(key=key, client=client, last_used=self._clock())

    def _release_holder(self, entry: _Entry, holder_owner: int) -> bool:
        with self._critical(None) as closers:
            entry.holders -= 1
            if entry.holders == 0:
                if entry.retired or self._stale(entry, self._clock()):
                    self._drop_locked(entry, closers)
                else:
                    entry.touch(self._clock())
            # Releasing is when a client becomes idle, and it is the only moment
            # the bound can be restored without waiting for the next acquire.
            self._evict_locked(self._clock(), closers)
            # Keep ownership registered until all required closes finish.
            with self._holders_changed:
                self._live_holders[holder_owner] -= 1
                if self._live_holders[holder_owner] == 0:
                    del self._live_holders[holder_owner]
                    self._holders_changed.notify_all()
                return self._closed and self._drained()

    def _defer_holder_release(
        self,
        entry: _Entry,
        holder_owner: int,
        submission: _Submission,
    ) -> None:
        def release_when_finished() -> None:
            submission.finished.wait()
            stop_io = self._release_holder(entry, holder_owner)
            if stop_io:
                self._io.stop()
            self._outstanding.release()

        # Only exceptional cancellations create a waiter. The semaphore slot
        # remains held, bounding these waiters to max_outstanding.
        threading.Thread(
            target=release_when_finished,
            name="kb-vector-retired-release",
            daemon=True,
        ).start()

    @contextmanager
    def lease(self, identity: str, deadline: Deadline | None = None) -> Iterator[LeasedClient]:
        """One client, reference counted, for the duration of one call.

        Yields a :class:`LeasedClient`: the pooled client and the credential
        revision it was built for, reachable only through this call's budget.
        The caller must not close it; releasing is the pool's business, and
        the pool closes it when eviction, rotation or shutdown says so.

        Every step is charged to the same budget now: the queue, the
        credential read, the pool lock, and initialization. Previously only the
        queue was, so a request with twenty milliseconds left could still spend
        a hundred and fifty of them waiting for a lock another thread was
        holding. An acquisition that cannot finish inside what is left returns
        a timeout having started nothing that reaches the network, and a
        credential read still running keeps this lease's slot and its pool
        registration until it really finishes.
        """
        if self._closed:
            raise VectorClientPoolClosed("vector client pool is shut down")

        if not self._acquire_slot(deadline):
            # The budget was gone before anything was started, so nothing was
            # sent. This is a timeout, not a cancelled request.
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)

        # From here this acquisition owns an outstanding slot and a pending
        # registration, so shutdown can see it and the outstanding bound counts
        # it, for as long as any part of it is still running.
        self._begin_acquisition()
        promoted = False
        deferred_read: concurrent.futures.Future | None = None
        entry: _Entry | None = None
        holder_owner = threading.get_ident()
        stop_io = False
        release_deferred = False
        try:
            try:
                secret, revision = self._read_key_bounded(identity, deadline)
            except _ReadOverran as overran:
                deferred_read = overran.future
                raise TimeoutError(INDEX_DEADLINE_EXCEEDED) from None
            key = self.key_for(identity, revision)

            with self._critical(deadline) as closers:
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
                    self._retire_locked(stale, closers)
                existing = self._entries.get(key)
                if existing is not None and not existing.retired and not self._stale(existing, now):
                    existing.touch(now)
                    entry = existing
                else:
                    if existing is not None:
                        # Idle past its TTL: replace it rather than hand back a
                        # connection that has been sitting since.
                        self._retire_locked(existing, closers)
                    # Built under the lock so two callers cannot both build a
                    # client for one key and leave one of them unowned. The
                    # build is itself bounded: a budget that has run out
                    # starts nothing at all.
                    entry = self._build_locked(key, secret, deadline, closers)
                    self._entries[key] = entry
                # Counted before eviction so the entry this call is about to
                # use is not the one eviction drops.
                entry.holders += 1
                with self._holders_changed:
                    self._live_holders[holder_owner] = self._live_holders.get(holder_owner, 0) + 1
                    # Pending becomes holder in one step, so shutdown never
                    # sees a gap in which an admission is neither.
                    promoted = True
                    self._pending -= 1
                    self._holders_changed.notify_all()
                self._evict_locked(now, closers)

            leased = LeasedClient(self, entry, revision, deadline)
            try:
                yield leased
            finally:
                submission = leased.unfinished_submission
                if submission is None:
                    stop_io = self._release_holder(entry, holder_owner)
                else:
                    # This lease's one holder and slot remain charged until the
                    # actual submission reaches its completion event.
                    release_deferred = True
                    self._defer_holder_release(entry, holder_owner, submission)
        finally:
            # Ownership of the outstanding slot, case by case. A caller that
            # stops waiting is not work that stopped, so the slot only changes
            # hands together with the work it was charged to.
            #
            #   deferred release    -> the deferred thread owns the slot
            #   promotion happened  -> this lease owns it, hand it back here
            #   read still running  -> the retention thread owns it, and the
            #                           registration, until the read ends
            #   nothing started     -> hand both back here
            if release_deferred:
                pass
            else:
                if stop_io:
                    self._io.stop()
                if promoted:
                    self._outstanding.release()
                elif deferred_read is not None:
                    # The credential read is still running in its worker. The
                    # slot and the registration stay charged to it until it
                    # finishes; its result is discarded, and no SDK work is
                    # ever started from it.
                    self._retain_slot_until_read(deferred_read)
                else:
                    self._end_acquisition()
                    self._outstanding.release()

    def shutdown(self) -> None:
        """Retire every client and stop the I/O loop when teardown is safe.

        Clients still in use are closed by the holder that releases them last.
        Shutdown waits up to ``IO_JOIN_SECONDS`` for live holders *and* for
        acquisitions that still own a slot -- a credential read that overran a
        caller's budget is exactly one of those -- when they belong to other
        threads; a holder calling shutdown in its own thread is reported
        immediately instead of waiting on itself. If work remains after the
        bounded wait, shutdown reports the still-open work and leaves the loop
        running. The last release then closes the retired client and stops the
        loop, so shutdown never closes or stops it under a live call.
        """
        with self._critical(None) as closers:
            self._closed = True
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                if entry.holders:
                    entry.retired = True
                else:
                    self._drop_locked(entry, closers)
        # The wait happens with no pool lock held, and only the drain lock.
        # Holding the pool lock across it would stop the releases and the
        # credential reads this shutdown is waiting for from ever finishing,
        # so the bounded wait would always expire.
        with self._holders_changed:
            same_thread = threading.get_ident() in self._live_holders
            still_open = self._outstanding_work()
            if still_open and not same_thread:
                self._holders_changed.wait_for(self._drained, timeout=IO_JOIN_SECONDS)
                still_open = self._outstanding_work()
        if still_open:
            _logger.warning(
                "vector client shutdown incomplete: %d acquisition(s) still open; "
                "I/O loop remains running until they finish",
                still_open,
            )
        else:
            self._io.stop()
            self._stop_readers()

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
