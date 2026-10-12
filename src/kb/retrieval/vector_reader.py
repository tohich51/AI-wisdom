"""Read-only OpenViking vector retrieval adapter."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from openviking_sdk import SyncHTTPClient

from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.secret_store import read_credentials


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
    ) -> None:
        self._url = url
        self._read_root = read_root
        self._client_factory = client_factory

    def find(
        self,
        identity: str,
        resource_root: str,
        query: str,
        limit: int,
    ) -> list[Candidate]:
        if (
            not isinstance(resource_root, str)
            or self._ROOT_PATTERN.fullmatch(resource_root) is None
        ):
            raise ValueError("invalid resource root")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        search_text = SELECTED_PROFILE.query_text(query)
        client = None
        try:
            try:
                key = read_credentials(self._read_root, identity)
                client = self._client_factory(
                    url=self._url,
                    api_key=key,
                    timeout=45,
                )
                client.initialize()
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
            finally:
                if client is not None:
                    client.close()

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
            raise IndexUnavailable("index_unavailable") from None
