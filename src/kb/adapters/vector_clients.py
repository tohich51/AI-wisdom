"""Bounded, shared OpenViking SDK clients for read-only retrieval.

Why this file exists, and what was *read* in the installed SDK rather than
assumed (openviking_sdk in the shared venv, at the accepted F, 2026-10-16):

* ``SyncHTTPClient`` holds one ``AsyncHTTPClient``, which builds exactly one
  ``httpx.AsyncClient`` inside ``initialize()``. Reusing that client is what
  the pool buys; constructing one per query -- the shape the accepted F has --
  is what it does not. A search fans out over up to twenty libraries, so the
  per-query shape paid twenty TLS handshakes and twenty ``initialize()`` calls
  before the first result.
* ``find()`` takes no timeout. The socket timeout is fixed when the client is
  constructed (``httpx.AsyncClient(timeout=self._timeout)``). A client shared
  between two requests therefore cannot carry either request's deadline, so
  the client's own timeout stays a *network ceiling* and the shared deadline is
  enforced here, around the call.
* ``run_async()`` does not build an event loop per call. It dispatches onto a
  single process-wide worker loop in a single daemon thread
  (``_get_worker_loop``) and blocks the calling thread on
  ``future.result()``. Three consequences are load-bearing here:

  - the SDK does not give real network parallelism. Work is serialised on one
    loop, so the honest bound is on *outstanding* calls, not on threads;
  - a caller that stops waiting has not cancelled anything. An expired
    deadline means this request stops *using* the result. It never means the
    provider was told to stop, and this module never reports it as such;
  - a client must not be closed while a call is still in flight, because that
    call is running on the shared loop. Eviction therefore defers the close
    until the last holder releases, instead of closing under a live call.

The key is ``(endpoint, account, profile, credential revision)``. The raw
secret is never a key and never reaches a log: ``credential_revision`` is a
truncated digest, held only in this process, and the entry holds the SDK client
that already carries the secret. Rotation is therefore a key change, and the
old entry is evicted and closed on its own.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from openviking_sdk import SyncHTTPClient

from kb.adapters.secrets import read_credentials
from kb.retrieval.embedding_profile import SELECTED_PROFILE

#: A network ceiling for one SDK call, in seconds. This is the timeout the
#: accepted F already passed to every client, kept so the wire behaviour of an
#: index call does not change when the client stops being rebuilt per query.
NETWORK_TIMEOUT = 45.0

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

#: Outstanding SDK calls. The SDK dispatches every call onto one process-wide
#: loop, so this bounds queue depth rather than parallelism, and the value is
#: set to cover the same fan-out for the same reason.
MAX_OUTSTANDING = 20

ClientKey = tuple[str, str, str, str]

#: Close failures are reported by type only. A close that fails can carry the
#: endpoint and, in some transports, the credential in its message, so the
#: message is never the thing that gets written down.
_logger = logging.getLogger(__name__)


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
    caller whose budget is already gone must not start one at all. The
    distinction matters because the SDK cannot cancel: giving up on the result
    is the only thing available, and it is recorded as a *timeout* rather than
    a cancellation.
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
        """The smaller of the network ceiling and what is left of the budget."""
        return max(0.0, min(float(ceiling), self.remaining))


@dataclass
class _Entry:
    key: ClientKey
    client: Any
    last_used: float
    holders: int = 0
    retired: bool = False

    def touch(self, now: float) -> None:
        self.last_used = now


class VectorClientCache:
    """A bounded, thread-safe pool of SDK clients with explicit lifecycle.

    Construction, eviction, rotation and shutdown all close the clients they
    drop. A client is only ever closed once its last holder has released it,
    so no close can land on a call that is still running on the SDK's shared
    loop.
    """

    def __init__(
        self,
        url: str,
        read_root,
        client_factory: Callable[..., Any] = SyncHTTPClient,
        *,
        max_clients: int = MAX_CLIENTS,
        ttl_seconds: float = CLIENT_TTL_SECONDS,
        max_outstanding: int = MAX_OUTSTANDING,
        network_timeout: float = NETWORK_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
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
        # A last resort so a process that exits without an explicit shutdown
        # does not leave httpx connection pools and sockets to be reclaimed
        # by the interpreter. Idempotent, and it only closes idle clients.
        atexit.register(self.shutdown)
        # Bounds how many SDK calls may be outstanding at once. The SDK
        # serialises them onto one loop regardless, so this is about queue
        # depth, not parallelism.
        self._outstanding = threading.BoundedSemaphore(max(1, int(max_outstanding)))

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
            entry.client.close()
        except Exception as exc:
            # A close failure is not a reason to fail a search.
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

    def _build_locked(self, key: ClientKey, secret: str, timeout: float) -> _Entry:
        client = self._factory(url=key[0], api_key=secret, timeout=timeout)
        client.initialize()
        return _Entry(key=key, client=client, last_used=self._clock())

    @contextmanager
    def lease(self, identity: str, deadline: Deadline | None = None) -> Iterator[tuple[Any, str]]:
        """One client, reference counted, for the duration of one call.

        Yields the client and the credential revision it was built for. The
        caller must not close it; releasing is the pool's business, and the
        pool closes it when eviction, rotation or shutdown says so.
        """
        secret, revision = self.read_key(identity)
        key = self.key_for(identity, revision)

        if not self._acquire_slot(deadline):
            # The budget was gone before the call started, so nothing was
            # sent. This is a timeout, not a cancelled request.
            raise TimeoutError("index_deadline_exceeded")

        entry: _Entry | None = None
        try:
            with self._lock:
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
                    timeout = (
                        deadline.timeout_for(self._network_timeout)
                        if deadline
                        else self._network_timeout
                    )
                    if timeout <= 0:
                        raise TimeoutError("index_deadline_exceeded")
                    # Built under the lock so two callers cannot both build a
                    # client for one key and leave one of them unowned.
                    entry = self._build_locked(key, secret, timeout)
                    self._entries[key] = entry
                # Counted before eviction so the entry this call is about to
                # use is not the one eviction drops.
                entry.holders += 1
                self._evict_locked(now)

            try:
                yield entry.client, revision
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
        finally:
            self._outstanding.release()

    def shutdown(self) -> None:
        """Close every client this pool owns.

        Clients still in use are retired and closed by the holder that
        releases them last, so shutdown never closes under a live call.
        """
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            if entry.holders:
                entry.retired = True
            else:
                self._close(entry)

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
