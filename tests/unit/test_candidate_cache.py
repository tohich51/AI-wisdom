"""Candidate reuse is bounded, cancellation-safe and contains no hydrated text."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from kb.adapters.vector_clients import Deadline
from kb.adapters.vector_reader import FrozenCandidate
from kb.retrieval.candidate_cache import CandidateCache


def test_ttl_lru_and_partial_failures_are_not_retained():
    now = [0.0]
    cache = CandidateCache(size=1, ttl=2, clock=lambda: now[0])
    calls = []
    library = uuid4()

    def compute():
        calls.append(1)
        return {library: [FrozenCandidate("viking://synthetic/a.md", 0.9)]}, set()

    def get(key="a"):
        return cache.get_or_compute(key, compute, Deadline(1))

    assert get() == get() and len(calls) == 1
    now[0] = 2
    get()
    assert len(calls) == 2
    get("b")
    get("a")
    assert len(calls) == 4

    def partial():
        return {}, {library}

    cache.get_or_compute("partial", partial, Deadline(1))
    assert cache.get_or_compute("partial", compute, Deadline(1))[1] == frozenset()
    with pytest.raises(ValueError):
        cache.get_or_compute("bad", lambda: (_ for _ in ()).throw(ValueError()), Deadline(1))
    assert cache.get_or_compute("bad", compute, Deadline(1)) is not None


def test_singleflight_waiter_deadline_and_close_do_not_cancel_owner():
    cache = CandidateCache(size=1)
    started, release = Event(), Event()
    calls = []

    def compute():
        calls.append(1)
        started.set()
        assert release.wait(3)
        return {}, set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        owner = workers.submit(cache.get_or_compute, "a", compute, Deadline(3))
        assert started.wait(1)
        assert cache.get_or_compute("a", compute, Deadline(0.01)) is None
        assert cache.get_or_compute("b", compute, Deadline(1)) is None
        waiter = workers.submit(cache.get_or_compute, "a", compute, Deadline(3))
        cache.close()
        assert waiter.result(timeout=1) is None
        release.set()
        assert owner.result(timeout=1) is not None
    assert len(calls) == 1
    assert cache.get_or_compute("a", compute, Deadline(1)) is None


def test_concurrent_same_key_shares_immutable_candidates():
    cache = CandidateCache()
    entered, release = Event(), Event()
    calls = []

    def compute():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {uuid4(): [FrozenCandidate("viking://synthetic/k.md", 0.8)]}, set()

    with ThreadPoolExecutor(max_workers=3) as workers:
        first = workers.submit(cache.get_or_compute, "same", compute, Deadline(3))
        assert entered.wait(1)
        second = workers.submit(cache.get_or_compute, "same", compute, Deadline(3))
        release.set()
        assert first.result(timeout=1) == second.result(timeout=1)
    assert len(calls) == 1
