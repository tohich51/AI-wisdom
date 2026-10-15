"""Read-only OpenViking vector retrieval adapter.

The validation and the response filter in this module are the security
boundary: a resource root that does not match the current embedding profile
never reaches the network, and nothing that comes back is trusted until it has
been matched against that root. The accepted F also rebuilt an SDK client for
every single call. That is what the shared, bounded pool in
:mod:`kb.adapters.vector_clients` replaced, and the two changes are the same
change: the pool is created here, from the same arguments the gateway already
passes, so no caller outside this lease had to be edited to get it.
"""

import math
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openviking_sdk import SyncHTTPClient

from kb.adapters.vector_clients import (
    CLIENT_TTL_SECONDS,
    MAX_CLIENTS,
    MAX_OUTSTANDING,
    NETWORK_TIMEOUT,
    Deadline,
    VectorClientCache,
)
from kb.retrieval.embedding_profile import SELECTED_PROFILE


@dataclass(frozen=True)
class FrozenCandidate:
    uri: str
    score: float


Candidate = FrozenCandidate


class IndexUnavailable(RuntimeError):
    """Raised when the vector index cannot be queried safely."""


class OpenVikingReader:
    _ROOT_PATTERN = re.compile(
        rf"^viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-[1-9][0-9]*$"
    )

    def __init__(
        self,
        url: str,
        read_root: Path,
        client_factory: Callable[..., SyncHTTPClient] = SyncHTTPClient,
        *,
        clients: VectorClientCache | None = None,
        network_timeout: float = NETWORK_TIMEOUT,
        max_clients: int = MAX_CLIENTS,
        ttl_seconds: float = CLIENT_TTL_SECONDS,
        max_outstanding: int = MAX_OUTSTANDING,
    ) -> None:
        self._url = url
        self._read_root = read_root
        self._client_factory = client_factory
        self._network_timeout = network_timeout
        self._max_clients = max_clients
        self._ttl_seconds = ttl_seconds
        self._max_outstanding = max_outstanding
        self._clients = clients
        self._pool_lock = threading.Lock()

    @property
    def pool(self) -> VectorClientCache:
        """The shared client pool, created on first use.

        Built lazily rather than in ``__init__`` so constructing a reader
        opens nothing: the gateway builds one during lifespan start-up, and a
        reader that is never used should not have initialised a client. Two
        threads racing here produce one pool, not two, because the assignment
        happens under the lock.
        """
        if self._clients is None:
            with self._pool_lock:
                if self._clients is None:
                    self._clients = VectorClientCache(
                        self._url,
                        self._read_root,
                        self._client_factory,
                        max_clients=self._max_clients,
                        ttl_seconds=self._ttl_seconds,
                        max_outstanding=self._max_outstanding,
                        network_timeout=self._network_timeout,
                    )
        return self._clients

    def find(
        self,
        identity: str,
        resource_root: str,
        query: str,
        limit: int,
        *,
        deadline: Deadline | None = None,
    ) -> list[Candidate]:
        # Validation stays outside the sanitising block: a malformed root or
        # limit is a caller bug, not an index outage, and must not be reported
        # as IndexUnavailable or the library would silently fall back.
        if (
            not isinstance(resource_root, str)
            or self._ROOT_PATTERN.fullmatch(resource_root) is None
        ):
            raise ValueError("invalid resource root")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        search_text = SELECTED_PROFILE.query_text(query)
        try:
            # lease() reads the credential first, so an identity that has no
            # key fails here without a client ever being built. It also bounds
            # how many calls are outstanding and closes the client when the
            # key changes, which is what makes rotation safe.
            with self.pool.lease(identity, deadline) as (client, _revision):
                payload = client.find(
                    search_text,
                    target_uri=resource_root,
                    limit=limit,
                    options={
                        "context_type": "resource",
                        "level": 2,
                        "read_content": False,
                    },
                )

            if not isinstance(payload, dict):
                raise ValueError("invalid index response")
            resources = payload.get("resources")
            if not isinstance(resources, list):
                raise ValueError("invalid index response")

            prefix = resource_root + "/"
            candidates: list[Candidate] = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                uri = resource.get("uri")
                score = resource.get("score")
                if not isinstance(uri, str) or not uri.startswith(prefix):
                    continue
                if isinstance(score, bool) or not isinstance(score, float):
                    continue
                if not math.isfinite(score):
                    continue
                if "level" in resource and resource["level"] != 2:
                    continue
                candidates.append(FrozenCandidate(uri=uri, score=score))
            return candidates[:limit]
        except Exception:
            # One sanitised failure for every reason: a provider error, a
            # malformed payload, an expired budget. The caller treats all of
            # them as "this library is not searchable right now" and keeps
            # lexical retrieval, which is the honest answer in each case.
            raise IndexUnavailable("index_unavailable") from None

    def close(self) -> None:
        """Close the clients this reader owns. Safe to call more than once."""
        if self._clients is not None:
            self._clients.shutdown()
