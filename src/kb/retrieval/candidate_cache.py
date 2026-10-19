"""Bounded process-local reuse of vector URIs/scores, never authorization or text."""

from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Future, TimeoutError
from threading import Lock
from time import monotonic
from uuid import UUID

from kb.adapters.vector_clients import Deadline
from kb.adapters.vector_reader import FrozenCandidate

# Tuples keep a consumer from mutating another request's cached candidates.
Result = tuple[tuple[tuple[UUID, tuple[FrozenCandidate, ...]], ...], frozenset[UUID]]


class CandidateCache:
    def __init__(self, *, size=64, ttl=120.0, clock=monotonic):
        if size < 1 or ttl <= 0:
            raise ValueError("invalid candidate cache bounds")
        self.size, self.ttl, self.clock = size, ttl, clock
        self._entries: OrderedDict[Hashable, tuple[float, Result]] = OrderedDict()
        self._pending: dict[Hashable, Future[Result | None]] = {}
        self._lock = Lock()
        self._closed = False

    def get_or_compute(
        self, key: Hashable, produce: Callable[[], tuple[dict, set]], deadline: Deadline
    ) -> Result | None:
        with self._lock:
            if self._closed or deadline.expired:
                return None
            now = self.clock()
            for expired in [k for k, (expiry, _) in self._entries.items() if expiry <= now]:
                del self._entries[expired]
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
                return entry[1]
            future = self._pending.get(key)
            owner = future is None
            if owner:
                if len(self._pending) >= self.size:
                    return None
                future = Future()
                self._pending[key] = future
        assert future is not None
        if not owner:
            try:
                return future.result(timeout=deadline.remaining)
            except TimeoutError:
                return None
        value = None
        try:
            candidates, unavailable = produce()
            value = (
                tuple((library, tuple(items)) for library, items in candidates.items()),
                frozenset(unavailable),
            )
            return value
        finally:
            with self._lock:
                if self._pending.get(key) is future:
                    del self._pending[key]
                    if value is not None and not value[1] and not deadline.expired:
                        self._entries[key] = (self.clock() + self.ttl, value)
                        while len(self._entries) > self.size:
                            self._entries.popitem(last=False)
                # close() may already have released all waiters.
                if not future.done():
                    future.set_result(value)

    def close(self):
        with self._lock:
            self._closed = True
            self._entries.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_result(None)
            self._pending.clear()
