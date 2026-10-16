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
from contextlib import ExitStack, contextmanager
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

#: How long a request without its own budget will wait for owned-client
#: capacity. Bounded on purpose: a permit comes back when a close lands, and a
#: close that is never going to land must not turn into a wait that never ends.
#: The value matches the close ceiling, so a close that is going to finish
#: normally gets its chance first.
CLIENT_CAPACITY_WAIT = CLOSE_TIMEOUT_SECONDS

#: How long one poll for a free client permit waits before the pool is asked
#: to make room. Short on purpose: the only thing that frees a permit is a
#: close landing, and waiting the whole budget for one would mean never asking.
CAPACITY_POLL_SECONDS = 0.05

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


class _UnfinishedAcquisition(Exception):
    """Internal: this acquisition's own work is still running after its budget.

    Carries the submission still in flight and the client it belongs to, so the
    pool can keep owning both. Never escapes the module: :meth:`lease` turns it
    into the ordinary ``TimeoutError`` a caller already knows, because from the
    caller's side that is exactly what happened -- the budget was missed.
    """

    def __init__(self, submission: Any, entry: Any) -> None:
        super().__init__("acquisition work outlived the deadline")
        self.submission = submission
        self.entry = entry


class VectorClientPoolClosed(RuntimeError):
    """A client was asked for after the pool had already been shut down."""


def _start_closer(target: Callable[[], None]) -> None:
    """Start one closer thread. A seam, so the failed-start path is testable."""
    threading.Thread(target=target, name="kb-vector-close", daemon=True).start()


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
        defer: Callable[[str, Callable[[], Any]], None] | None = None,
    ) -> None:
        self._grace = float(grace)
        self._join_timeout = float(join_timeout)
        # Where an unfinished submission goes. The pool supplies its existing
        # owner, so a cancelled call that has not unwound yet is charged and
        # finalised like every other piece of unfinished work, and the caller is
        # not kept waiting for it.
        self._defer = defer
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
        """The budget is gone: cancel, hand over, and return at once.

        The request used to wait out ``CANCEL_GRACE_SECONDS`` here, on top of
        the budget it had already missed -- so a twenty millisecond deadline
        with a one hundred and twenty millisecond grace returned at a hundred
        and forty, for a find and for an initialize alike. That wait is time
        the caller never budgeted for and cannot be charged to anyone.

        So the cancellation is scheduled, the submission is handed to the
        pool's existing owner, and the caller goes straight back. Ownership is
        what keeps the work safe, not the waiting: the entry is quarantined, the
        holder and its slot stay charged until the submission really unwinds,
        and the finalizer cannot run while the owner holds it. ``_grace`` is
        only a fallback for a dispatcher with no owner to hand to.
        """
        future.cancel()
        try:
            loop.call_soon_threadsafe(request_cancel)
        except RuntimeError:  # pragma: no cover - the loop stopped under us
            pass
        if submission.finished.is_set():
            # Already unwound, so the cancel was delivered and the call is
            # simply over. Whether it stopped *because* of the cancel is worth
            # recording: a task cancelled before its first step never observes
            # one, and neither should be claimed as cancelled on that basis.
            if not submission.cancelled:
                _logger.debug("vector call ended without observing cancellation")
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
        if self._defer is None:
            # No owner to hand to, so this is the only place the grace is still
            # worth spending.
            if not submission.await_unwound(self._grace):
                raise IndexCallStillOpen(
                    "index call did not stop after cancellation",
                    submission=submission,
                )
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
        self._defer("call-unwind", submission.finished.wait)
        raise IndexCallStillOpen(
            "index call did not stop after cancellation",
            submission=submission,
        )

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
    #: Whether this entry is holding one of the pool's total-owned-client slots.
    holds_slot: bool = False
    #: The transferred reservation whose finalizer returns this one slot.
    slot_cleanup: ExitStack | None = None
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
            # Under this lease's own budget: the call being cleaned up after has
            # already spent it, and another key's initialize must not be able to
            # hold this cleanup past the deadline.
            self._pool._quarantine(self._entry, self._deadline)
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
        # Closes that are really running. Counted like holders and pending
        # acquisitions, because a close that is still in flight is work this
        # pool started and has not finished: "drained" was true while one ran,
        # which is how a shutdown managed to stop the loop underneath it.
        self._closes = 0
        # Work this pool owns that is still running: an initialize that
        # refused cancellation, a close that outlived its grace, a
        # credential read that outlived a budget. One charge for all of
        # them, released only by the single finalizer below.
        self._deferred = 0
        # Clients that were dropped and could not be closed. Nothing is
        # running for them, so they do not block a drain, but a shutdown
        # that reports "drained" has to say they were left behind.
        self._abandoned_closes = 0
        # The finalizer runs at most once, so a shutdown that already
        # returned and a completion that arrives after it agree on one
        # outcome instead of each tearing down in turn.
        self._finalized = False
        self._readers: concurrent.futures.ThreadPoolExecutor | None = None
        self._readers_lock = threading.Lock()
        self._closed = False
        self._io = _IODispatcher(grace=cancel_grace, defer=self._own_work)
        # A last resort for a process that exits without an explicit shutdown,
        # so httpx connection pools and sockets are not left to the
        # interpreter's reclaimer. Idempotent, and never the primary path:
        # the gateway's lifespan calls shutdown() directly.
        atexit.register(self.shutdown)
        # Bounds how many SDK calls may be in flight at once.
        self._outstanding = threading.BoundedSemaphore(max(1, int(max_outstanding)))
        # Bounds how many SDK clients exist at all, not merely how many are in
        # the pool. max_clients alone counted only what _entries held, so a
        # rotation that retired a client and handed its close to a background
        # thread built a replacement immediately: with a pool of one and one
        # outstanding call, forty rotations left forty unclosed clients and
        # thirty-nine background closes, all invisible to the pool's own bound.
        # Taken before a client is constructed and held until its close really
        # lands -- or until a cancelled close really unwinds.
        self._client_slots = threading.BoundedSemaphore(self._max_clients)

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

    def _run_close(self, entry: _Entry):
        """Close one client. Returns its submission while it is still running.

        A close that was cancelled and has not unwound yet is not a close, and
        the submission is what says so. It is handed back instead of being
        swallowed, because a caller that swallowed it went on to treat the
        client as closed while the coroutine was still holding its socket, and
        then stopped the loop underneath it.
        """
        try:
            self._io.run(entry.client.close, CLOSE_TIMEOUT_SECONDS)
        except IndexCallStillOpen as still_open:
            return still_open.submission
        except Exception as exc:
            # A close failure is not a reason to fail a search, and after a
            # shutdown it is not even a reason to restart the loop. It is
            # logged by type: a close message can carry the endpoint, and in
            # some transports the credential. Nothing is running any more, but
            # the client is gone without being closed, and that is recorded
            # rather than left for a drained pool to imply otherwise.
            _logger.debug("vector client close failed: %s", type(exc).__name__)
            self._abandoned_closes += 1
        return None

    def _release_client_slot(self, entry: _Entry) -> None:
        """Give back the capacity an owned client was holding.

        Only ever reached once, because the entry is marked as no longer
        holding a slot at the same moment, and only once the client is really
        gone: a close that completed, or a close that was cancelled and has
        finally unwound.
        """
        cleanup = entry.slot_cleanup
        if cleanup is None:
            return
        entry.slot_cleanup = None
        entry.holds_slot = False
        cleanup.close()

    def _close_entry(self, entry: _Entry) -> None:
        """Close a dropped client, keeping the pool's ownership until it ends.

        A close that was cancelled and did not stop is still running, so the
        pool keeps owning it until it really unwinds -- otherwise the finalizer
        can stop the I/O loop under a coroutine that is still on it. And once it
        has unwound, no close is coming: the coroutine was cancelled, so this
        client is dropped without ever having been closed. That is counted, so
        a drained pool does not read as though every dropped client was shut.
        """
        submission = self._run_close(entry)
        if submission is None:
            # Closed, or failed outright: either way the client is gone and the
            # capacity it was holding can go back.
            self._release_client_slot(entry)
            return

        def record_after_unwind() -> None:
            with self._lock:
                self._abandoned_closes += 1
            self._release_client_slot(entry)
            _logger.debug("vector client close was cancelled; client dropped unclosed")

        # A cancelled close still holds its capacity until it has unwound, so a
        # storm of them cannot turn into a storm of live clients.
        self._own_work("close-unwind", submission.finished.wait, record_after_unwind)

    def _close(self, entry: _Entry) -> None:
        self._close_entry(entry)

    def _close_all(self, entries: list[_Entry]) -> None:
        """Hand a batch of dropped clients to a closer and return at once.

        A close is a round trip to the I/O loop and is the pool's own
        housekeeping, so it is never spent on a caller's budget: a rotation
        that retires another identity's client must not make this caller
        return after its own deadline. The entries are counted as outstanding
        work before the thread starts and stay owned by the pool until their
        closes actually land, so nothing can reuse them, close them a second
        time, or stop the loop while a close is on it.

        One thread per batch, not per client: a burst of evictions is one
        closer, and the batch is bounded by the pool's own client bound.

        This must be called with the pool lock still held. The batch is counted
        before the caller releases it, because a shutdown arriving in the gap
        between the unlock and the count would see every counter at zero and
        stop the I/O loop under a close that had been chosen but not started.
        """
        if not entries:
            return
        with self._holders_changed:
            self._closes += 1
            self._holders_changed.notify_all()

        def close_batch() -> None:
            try:
                for entry in entries:
                    self._close_entry(entry)
            finally:
                with self._holders_changed:
                    self._closes -= 1
                    self._holders_changed.notify_all()
                # The last close out is also the last moment at which the pool
                # may be drained, so the finalizer is consulted here too. A
                # close that outlived its grace has already charged itself as
                # deferred work, so it keeps the pool alive until it unwinds.
                self._finalize()

        try:
            _start_closer(close_batch)
        except Exception as exc:
            # No closer will run, so the reservation has to be handed back:
            # otherwise the counter never returns to zero and the pool never
            # drains. Nothing is in flight for these clients, so they cannot
            # keep the pool open, but they were dropped and never closed, and
            # that is counted and reported rather than left for a drained pool
            # to imply otherwise. By type only: a handoff failure can carry
            # the endpoint.
            with self._holders_changed:
                self._closes -= 1
                self._abandoned_closes += len(entries)
                self._holders_changed.notify_all()
            _logger.error(
                "vector client close handoff failed for %d client(s): %s",
                len(entries),
                type(exc).__name__,
            )
            self._finalize()

    def _own_work(
        self,
        label: str,
        work: Callable[[], Any],
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Own work that outlived its caller, until the work really ends.

        Every unfinished operation in this module comes through here: an
        initialize that refused cancellation, a close that outlived its grace, a
        credential read that outlived a caller's budget. The pool keeps the work
        charged for the whole of that time, so a drain check cannot call the
        pool empty while it is still going, and the single finalizer is
        consulted the moment it ends. Nothing here is waited on by the request
        that started the work.

        ``on_done`` runs before the charge is handed back, which is what lets
        one path both own an operation and release the slot and registration
        that were charged to it.
        """
        with self._holders_changed:
            self._deferred += 1
            self._holders_changed.notify_all()

        def run() -> None:
            try:
                work()
            except Exception as exc:
                # By type only: a close or read message can carry the endpoint
                # or the path, and neither belongs in a log line.
                _logger.debug("deferred %s failed: %s", label, type(exc).__name__)
            finally:
                try:
                    if on_done is not None:
                        on_done()
                except Exception as exc:
                    # A release step that fails must not strand the charge. A
                    # stranded charge means the pool can never drain, so the
                    # finalizer never runs, and every teardown after it waits
                    # out its whole timeout: one raised exception becomes a run
                    # that appears to hang.
                    _logger.debug("deferred %s release failed: %s", label, type(exc).__name__)
                with self._holders_changed:
                    self._deferred -= 1
                    self._holders_changed.notify_all()
                self._finalize()

        threading.Thread(target=run, name=f"kb-vector-{label}", daemon=True).start()

    def _finalize(self) -> None:
        """Shut the pool down once, when nothing it started is still running.

        The one place anything is stopped, and it stops both the I/O loop and
        the credential executor together. Every late completion comes through
        it -- a deferred credential read, the last holder, a close that
        outlived its grace, an initialize that refused cancellation -- so none
        of them can leave the pool half shut after a shutdown that had already
        returned. Idempotent, because both that shutdown and the completion
        that follows it call it.

        The pool lock is held across the decision and the teardown, because
        work is registered under that same lock; deciding outside it would
        leave a window in which a thread mid-release registers after the check
        and before the stop. The lock cannot deadlock against either join: the
        I/O loop only runs SDK coroutines and the credential workers only read
        a file, and neither asks for the pool lock.

        The ``_closed`` pre-check is not an optimisation. Every lease release
        calls this, and taking the pool lock unconditionally put an unbounded
        wait for it back onto the request path.
        """
        if not self._closed:
            return
        with self._lock:
            if self._finalized or not self._drained():
                return
            self._finalized = True
            self._io.stop()
            self._stop_readers()

    def _maybe_stop_io(self) -> None:
        """Stop the I/O loop if the pool is shut down and fully drained.

        Only ever called after deferred work has been *registered*, never
        before. A close or a release started by the current call has to be
        counted first, or this decides the pool is drained while its own
        cleanup is still running -- which is the failure this replaced.

        The pool lock is held across the decision *and* the stop, because
        registration happens under that same lock. Deciding outside it would
        leave a window in which a thread that is mid-release registers a close
        after the check and before the stop, which is the same bug one level
        down. The lock cannot deadlock against the join: the I/O loop only
        runs SDK coroutines and never asks for the pool lock.

        The ``_closed`` pre-check is not an optimisation. Every lease release
        calls this, and taking the pool lock unconditionally put an unbounded
        wait for that lock back onto the request path -- the exact defect the
        acquisition repair removed. The loop is only ever stopped after
        shutdown, so a live pool answers from a plain bool and never touches
        the lock.
        """
        if not self._closed:
            return
        with self._lock:
            if self._closed and self._drained():
                self._io.stop()

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

    def _critical_soon(self, deadline: Deadline | None) -> bool:
        """Take the pool lock now if this request's budget still allows it.

        Post-call bookkeeping needs this lock, and the lock can be held by
        another key's initialization. Waiting for it unconditionally is how a
        request that had already done its work ended up blocked a hundred
        milliseconds past a thirty millisecond budget.
        """
        if deadline is None:
            self._lock.acquire()
            return True
        return self._acquire_pool_lock(deadline)

    def _quarantine(self, entry: _Entry, deadline: Deadline | None = None) -> None:
        """Retire a client whose call did not stop, without waiting past a budget."""
        if not self._critical_soon(deadline):
            # The lock is busy with someone else's lifecycle work. Owning the
            # quarantine is enough; nothing waits on it.
            self._own_work("quarantine", lambda: self._quarantine(entry, None))
            return
        # Counted under the lock, exactly as _critical does. Registering the
        # close after the unlock is this cause, on its second attempt: a
        # shutdown arriving in between sees every counter at zero, stops the
        # loop, and the client is never closed at all.
        try:
            self._close_all(self._retire_now(entry))
        finally:
            self._lock.release()

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

        Yields the list of clients the caller wants closed. They are counted
        as outstanding work and their closer started before the lock is
        released, and the closes themselves run after it is.
        """
        if not self._acquire_pool_lock(deadline):
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
        closers: list[_Entry] = []
        try:
            yield closers
        finally:
            try:
                self._close_all(closers)
            finally:
                self._lock.release()

    def _drained(self) -> bool:
        """Whether nothing this pool started is still outstanding.

        Read without the drain lock. It is a predicate over three counters and
        the only consequence of a torn read is one extra turn of the wait loop
        in :meth:`shutdown`, which is bounded and re-evaluates anyway.
        """
        return (
            not self._live_holders and not self._pending and not self._closes and not self._deferred
        )

    def _outstanding_work(self) -> int:
        """Everything this pool has started and has not finished."""
        return sum(self._live_holders.values()) + self._pending + self._closes + self._deferred

    def _begin_acquisition(self) -> None:
        """Register work that holds a slot but has no entry yet."""
        with self._holders_changed:
            self._pending += 1

    def _end_acquisition(self) -> None:
        """Unregister it, wake a shutdown waiting for it, then finalise.

        The finalizer comes after the decrement, deliberately. Calling it
        before would see this work still charged, decide the pool was not
        drained, and then have nothing left to call it again -- so the last
        work item to finish could leave the I/O loop and the credential
        executor running for good, with only another shutdown able to stop
        them. It is called outside the drain lock because the order is always
        pool lock then drain lock, and the finalizer takes the pool lock.
        """
        with self._holders_changed:
            if self._pending:
                self._pending -= 1
            self._holders_changed.notify_all()
        self._finalize()

    def _ensure_readers(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._readers_lock:
            if self._readers is None:
                if self._closed:
                    # Refuse rather than resurrect. The admission check makes
                    # this hard to reach, and that is the point: a credential
                    # executor rebuilt after shutdown is a thread the gateway's
                    # completed shutdown does not own.
                    raise VectorClientPoolClosed("vector client pool is shut down")
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
        """Hold this acquisition's slot and registration until the read ends.

        Routed through the same owner as every other unfinished operation, so
        the read finishing calls the one finalizer and cannot leave the pool
        half shut after a shutdown that already returned.
        """

        def release_when_read_finished() -> None:
            future.result()

        def release_charge() -> None:
            self._end_acquisition()
            self._outstanding.release()

        self._own_work("credential-read", release_when_read_finished, release_charge)

    def _retain_unfinished_acquisition(self, unfinished: _UnfinishedAcquisition) -> None:
        """Hand this acquisition's charge to whoever waits for the real unwind.

        The client is closed only after the initialize has actually unwound,
        which is the whole point: a close issued now would race the coroutine
        that is still initializing it.
        """
        entry = unfinished.entry

        def release_acquisition() -> None:
            self._close_entry(entry)
            self._end_acquisition()
            self._outstanding.release()

        self._own_work("init-unwind", unfinished.submission.finished.wait, release_acquisition)

    def _may_reuse(self, key: ClientKey) -> bool:
        """Whether an entry for this key looks reusable right now.

        A snapshot taken without the lock, and only to decide whether capacity
        has to be taken before entering the critical section. The decision is
        re-made under the lock, and a permit taken on a wrong guess is handed
        straight back.
        """
        entry = self._entries.get(key)
        return entry is not None and not entry.retired and not self._stale(entry, self._clock())

    def _acquire_client_capacity(self, deadline: Deadline | None) -> bool:
        """Take one of the pool's total-owned-client permits, or refuse.

        Never called with the pool lock held. Waiting for a permit while
        holding it is how a full cache stops being able to evict the idle entry
        whose close is what would free one, and with no caller deadline a
        permit that never comes is a wait that never ends. So the wait is
        sliced, and each time a slice runs out the pool is asked to drop what it
        can before the next one: the close started there is what returns the
        permit, so the loop makes progress rather than repeating itself.
        """
        if self._client_slots.acquire(blocking=False):
            return True
        budget = (
            CLIENT_CAPACITY_WAIT if deadline is None else deadline.timeout_for(CLIENT_CAPACITY_WAIT)
        )
        end = time.monotonic() + budget
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            # A short poll, not the whole budget: this catches a permit that a
            # concurrent close has just freed, and anything still missing is
            # dealt with below rather than waited out.
            if self._client_slots.acquire(
                blocking=True, timeout=min(remaining, CAPACITY_POLL_SECONDS)
            ):
                return True
            # Nothing freed. Ask the pool to make room: an idle entry dropped
            # here has its close started, and that close is what returns the
            # permit. Every entry busy means there is nothing to drop, and the
            # owned cap is then the honest answer rather than a spin.
            # Bounded like the wait it sits in. Another thread doing
            # lifecycle work under the lock must not turn this into a wait
            # with no end of its own; the slice is spent and the loop tries
            # again.
            remaining_capacity_wait = end - time.monotonic()
            if remaining_capacity_wait <= 0:
                return False
            lock_budget = min(CAPACITY_POLL_SECONDS, remaining_capacity_wait)
            if not self._acquire_pool_lock(Deadline(lock_budget)):
                continue
            try:
                closers: list[_Entry] = []
                if not self._make_room_locked(closers):
                    return False
                # Counted under the lock, as everywhere else.
                self._close_all(closers)
            finally:
                self._lock.release()

    def _build_locked(
        self,
        key: ClientKey,
        secret: str,
        deadline: Deadline | None,
        reservation: ExitStack,
        closers: list[_Entry],
    ) -> _Entry:
        # Capacity was taken by the caller, before the critical section, so
        # the bound counts clients that are still being built and not just the
        # ones already pooled -- and so the wait for it never holds this lock.
        # The timeout here is the fixed network ceiling and nothing else. It
        # does not consult the caller's budget, because a client is shared: a
        # timeout derived from one request's deadline is inherited by every
        # later request that reuses the client.
        # ``reservation`` owns the caller's release until construction succeeds.
        # A factory failure leaves it to unwind; a constructed client transfers
        # that cleanup to its entry before budget validation or initialization.
        client = self._factory(url=key[0], api_key=secret, timeout=self._network_timeout)
        entry = _Entry(key=key, client=client, last_used=0.0, holds_slot=True)
        # The factory has returned, so transfer the one reservation now. The
        # popped stack follows this entry through validation, initialization,
        # cancellation, and close, and is run only after the client is gone.
        entry.slot_cleanup = reservation.pop_all()
        try:
            entry.touch(self._clock())
            budget = (
                CLOSE_TIMEOUT_SECONDS
                if deadline is None
                else deadline.timeout_for(CLOSE_TIMEOUT_SECONDS)
            )
            if budget <= 0:
                raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
            self._io.run(client.initialize, budget)
        except IndexCallStillOpen as still_open:
            # The initialize ignored its cancellation and is still running, so
            # this client is not merely half-built: it is a client in the
            # middle of becoming one. Queuing a close for it right now would
            # race that coroutine, and the client could finish initializing
            # after the only close that was meant to catch it and be lost.
            # Ownership passes to the caller instead, which keeps this
            # acquisition charged and closes the client after the real unwind.
            entry.closed = True
            raise _UnfinishedAcquisition(still_open.submission, entry) from None
        except BaseException:
            # Anything else means nothing is running on the client, including
            # an expired validation budget, so queue its close with the rest.
            # Its transferred reservation is released by that close, not here.
            entry.closed = True
            closers.append(entry)
            raise
        return entry

    def _release_holder_locked(
        self, entry: _Entry, holder_owner: int, closers: list[_Entry]
    ) -> None:
        """The holder bookkeeping. Runs with the pool lock already held."""
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

    def _make_room_locked(self, closers: list[_Entry]) -> bool:
        """Drop the least recently used idle entry, to start its close.

        Reached only when no client permit is free, which means the pool is at
        its owned cap. Dropping an entry does not return its permit -- the close
        does, once it lands -- but it is what starts that close, so this is the
        step that turns a full cache into a draining one. A busy entry is never
        dropped, so this cannot close under a call that is still running.
        """
        idle = [e for e in self._entries.values() if e.holders == 0 and not e.closed]
        if not idle:
            return False
        self._drop_locked(min(idle, key=lambda e: e.last_used), closers)
        return True

    def _retire_now(self, entry: _Entry) -> list[_Entry]:
        """Retire one entry with the pool lock already held. Returns closers."""
        closers: list[_Entry] = []
        self._retire_locked(entry, closers)
        return closers

    def _release_holder_now(self, entry: _Entry, holder_owner: int) -> list[_Entry]:
        """Release one holder with the pool lock already held. Returns closers."""
        closers: list[_Entry] = []
        self._release_holder_locked(entry, holder_owner, closers)
        return closers

    def _release_holder(self, entry: _Entry, holder_owner: int) -> None:
        """Unbounded release, for the paths the pool runs on its own threads."""
        with self._critical(None) as closers:
            self._release_holder_locked(entry, holder_owner, closers)
        self._finalize()

    def _release_holder_bounded(
        self, entry: _Entry, holder_owner: int, deadline: Deadline | None
    ) -> bool:
        """Release at this request's budget, or hand the release to an owner.

        A request that has finished its work must not then queue behind another
        key's initialization. When the budget is gone the release is owned
        instead, and the outstanding slot it was charged stays charged until
        the bookkeeping it pays for has actually happened -- handing the slot
        back early would let the pool admit more work than its bound allows
        while that bookkeeping is still outstanding.
        """
        if not self._critical_soon(deadline):
            self._own_work(
                "holder-release",
                lambda: self._release_holder(entry, holder_owner),
                self._outstanding.release,
            )
            return False
        # Counted under the lock, for the same reason as the quarantine
        # above and for the same cause.
        try:
            self._close_all(self._release_holder_now(entry, holder_owner))
        finally:
            self._lock.release()
        self._finalize()
        return True

    def _defer_holder_release(
        self,
        entry: _Entry,
        holder_owner: int,
        submission: _Submission,
    ) -> None:
        def release_when_finished() -> None:
            submission.finished.wait()
            self._release_holder(entry, holder_owner)
            # The one finalizer, so a completion that arrives after a bounded
            # shutdown returned still tears the pool down completely.
            self._finalize()
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
        # A fast path only. The authoritative check happens under the pool lock
        # together with the registration below, because a check that is not
        # atomic with the closed transition is not a check: this thread can be
        # descheduled here, shutdown can finish, and the next line would admit
        # work into a pool that has already torn itself down.
        if self._closed:
            raise VectorClientPoolClosed("vector client pool is shut down")

        if not self._acquire_slot(deadline):
            # The budget was gone before anything was started, so nothing was
            # sent. This is a timeout, not a cancelled request.
            raise TimeoutError(INDEX_DEADLINE_EXCEEDED)

        # From here this acquisition owns an outstanding slot and a pending
        # registration, so shutdown can see it and the outstanding bound counts
        # it, for as long as any part of it is still running.
        try:
            # Bounded as well as atomic. Sharing the lock with shutdown is what
            # makes the check mean something, but taking it with no bound would
            # put back the very wait the acquisition repair removed: the lock
            # is held here for a bool and a counter, so a budget that is gone
            # must not be spent on it.
            if not self._acquire_pool_lock(deadline):
                raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
            try:
                # Shutdown sets _closed under this same lock, so either this
                # thread registers before that and shutdown waits for it, or it
                # registers after and is refused. There is no window in which
                # both are true, which is what let a late waiter take a slot
                # and then build a fresh credential executor over a pool that
                # had already finished shutting down.
                if self._closed:
                    raise VectorClientPoolClosed("vector client pool is shut down")
                with self._holders_changed:
                    self._pending += 1
                    self._holders_changed.notify_all()
            finally:
                self._lock.release()
        except BaseException:
            self._outstanding.release()
            raise
        promoted = False
        handed_over = False
        deferred_read: concurrent.futures.Future | None = None
        entry: _Entry | None = None
        holder_owner = threading.get_ident()
        release_deferred = False
        try:
            try:
                secret, revision = self._read_key_bounded(identity, deadline)
            except _ReadOverran as overran:
                deferred_read = overran.future
                raise TimeoutError(INDEX_DEADLINE_EXCEEDED) from None
            key = self.key_for(identity, revision)

            # Capacity first, outside the critical section. Holding the pool
            # lock while waiting for a permit means a cache that is full cannot
            # evict the idle entry whose close would free one, so the wait can
            # never finish. If the entry turns out to be reusable after all --
            # another caller may have won the race -- the permit goes straight
            # back. Until construction succeeds, the reservation context owns
            # it; construction transfers that cleanup to the new entry.
            needs_capacity = not self._may_reuse(key)
            if needs_capacity and not self._acquire_client_capacity(deadline):
                raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
            reservation = ExitStack()
            if needs_capacity:
                reservation.callback(self._client_slots.release)
            try:
                with reservation, self._critical(deadline) as closers:
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
                    if (
                        existing is not None
                        and not existing.retired
                        and not self._stale(existing, now)
                    ):
                        existing.touch(now)
                        entry = existing
                        # Any speculative permit belongs to ``reservation`` and
                        # is returned when the surrounding context exits.
                    else:
                        if existing is not None:
                            # Idle past its TTL: replace it rather than hand back a
                            # connection that has been sitting since.
                            self._retire_locked(existing, closers)
                        if not needs_capacity:
                            # The optimistic hit can change before this locked
                            # recheck. Every build must own one permit, but waiting
                            # for it under the pool lock is forbidden.
                            if not self._client_slots.acquire(blocking=False):
                                raise TimeoutError(INDEX_DEADLINE_EXCEEDED)
                            reservation.callback(self._client_slots.release)
                        # Built under the lock so two callers cannot both build a
                        # client for one key and leave one of them unowned. The
                        # build is itself bounded: a budget that has run out
                        # starts nothing at all.
                        # The entry takes over the permit this thread already
                        # holds, so the bound counts a client that is still
                        # being built.
                        entry = self._build_locked(key, secret, deadline, reservation, closers)
                        self._entries[key] = entry
                    # Counted before eviction so the entry this call is about to
                    # use is not the one eviction drops.
                    entry.holders += 1
                    with self._holders_changed:
                        self._live_holders[holder_owner] = (
                            self._live_holders.get(holder_owner, 0) + 1
                        )
                        # Pending becomes holder in one step, so shutdown never
                        # sees a gap in which an admission is neither.
                        promoted = True
                        self._pending -= 1
                        self._holders_changed.notify_all()
                    self._evict_locked(now, closers)
            except _UnfinishedAcquisition as unfinished:
                # Its own work is still running, so the slot and the
                # registration this acquisition holds now belong to the
                # owner that will close the client after the real unwind.
                # Nothing below may hand them back.
                self._retain_unfinished_acquisition(unfinished)
                # The entry carries the permit now, so the owner that will close
                # it after the real unwind is what gives it back.
                handed_over = True
                # What the caller sees is unchanged and true: the budget
                # was missed, and nothing is reported as cancelled.
                raise TimeoutError(INDEX_DEADLINE_EXCEEDED) from None

            leased = LeasedClient(self, entry, revision, deadline)
            try:
                yield leased
            finally:
                submission = leased.unfinished_submission
                if submission is None:
                    if not self._release_holder_bounded(entry, holder_owner, deadline):
                        # The owner of the deferred release also owns this
                        # lease's outstanding slot until it has run.
                        release_deferred = True
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
                self._finalize()
            else:
                # After the release, so any close it started is already counted.
                if handed_over:
                    # The owner of the unfinished work holds this acquisition's
                    # slot and registration now. Releasing them here would let
                    # the pool admit more work than the bound allows, which is
                    # the capacity the deferred owner is holding.
                    pass
                elif promoted:
                    self._outstanding.release()
                elif deferred_read is not None:
                    # The credential read is still running in its worker. The
                    # slot and the registration stay charged to it until it
                    # finishes; its result is discarded, and no SDK work is
                    # ever started from it.
                    self._retain_slot_until_read(deferred_read)
                    self._finalize()
                else:
                    # _end_acquisition finalises after its own decrement, which
                    # is the last one this lease can cause.
                    self._end_acquisition()
                    self._outstanding.release()
                    self._finalize()

    def shutdown(self) -> None:
        """Retire every client and stop the I/O loop when teardown is safe.

        Clients still in use are closed by the holder that releases them last.
        Shutdown waits up to ``IO_JOIN_SECONDS`` for three kinds of outstanding
        work belonging to other threads: live holders, acquisitions that still
        own a slot (a credential read that overran a caller's budget is exactly
        one of those), and closes that are really running. A holder calling
        shutdown in its own thread is reported immediately instead of waiting
        on itself. If work remains after the bounded wait, shutdown reports
        the still-open work and leaves the loop running. The last release then
        closes the retired client and stops the loop, so shutdown never closes
        or stops it under a live call or under a close that is still in
        flight.
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
                "the pool is left running until they finish",
                still_open,
            )
        if self._abandoned_closes:
            # Said out loud, because "drained" on its own would read as though
            # every dropped client had been closed. Nothing is in flight for
            # them, so they do not hold the pool open; they are simply gone.
            _logger.warning(
                "%d client(s) were dropped and could not be closed; they are no longer pooled",
                self._abandoned_closes,
            )
        # The one finalizer. If anything is still outstanding it does nothing
        # and that work calls it again when it ends, so a completion after this
        # return still tears the pool down completely.
        self._finalize()

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
