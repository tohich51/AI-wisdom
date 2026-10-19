"""Exact, verified vectors-only writes; exclusively used by the indexer."""

import re
from functools import wraps
from uuid import UUID

from openviking_sdk import SyncHTTPClient
from openviking_sdk.errors import AlreadyExistsError, ConflictError

from kb.retrieval.embedding_profile import SELECTED_PROFILE


def safe_write(method):
    @wraps(method)
    def call(self, root, *args):
        if not re.fullmatch(
            rf"viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-[1-9][0-9]*", root
        ):
            raise ValueError("invalid_index_root")
        try:
            return method(self, root, *args)
        except Exception:
            raise RuntimeError("index_writer_unavailable") from None

    return call


class OpenVikingWriter:
    def __init__(self, url, key):
        self.client = SyncHTTPClient(url=url, api_key=key, timeout=600)
        self.client.initialize()
        self._errors = self._error_count(self.client.wait_processed(timeout=600))

    @staticmethod
    def _error_count(value):
        if isinstance(value, dict):
            return sum(
                (
                    item
                    if key == "error_count" and isinstance(item, int)
                    else OpenVikingWriter._error_count(item)
                )
                for key, item in value.items()
            )
        if isinstance(value, list):
            return sum(OpenVikingWriter._error_count(item) for item in value)
        return 0

    def _wait(self):
        errors = self._error_count(self.client.wait_processed(timeout=600))
        if errors > self._errors:
            self._errors = errors
            raise RuntimeError("index_processing_failed")
        self._errors = errors

    @safe_write
    def verify(self, root, documents):
        self._wait()
        report = self.client.check_consistency(root)
        if (
            report.get("ok") is not True
            or report.get("missing_record_count") != 0
            or report.get("expected_count", 0) < len(documents)
        ):
            raise RuntimeError("index_vector_coverage_failed")
        for id, text in documents:
            if self.client.read(f"{root}/{id}.md") != text:
                raise RuntimeError("index_document_verification_failed")

    def close(self):
        self.client.close()

    @safe_write
    def prepare(self, root):
        try:
            self.client.mkdir(root, description="Knowledge generation")
        except (AlreadyExistsError, ConflictError):
            pass
        self._wait()

    @safe_write
    def write_batch(self, root, documents):
        if len(documents) > 16 or any(
            not isinstance(id, UUID) or not isinstance(text, str) or not text.strip()
            for id, text in documents
        ):
            raise ValueError("invalid_index_documents")
        uris = []
        for id, text in documents:
            uri = f"{root}/{id}.md"
            self.client.write(uri, text, wait=False, options={"processing_mode": "vectors_only"})
            uris.append(uri)
        self._wait()
        for uri, (_, text) in zip(uris, documents, strict=True):
            if self.client.read(uri) != text:
                raise RuntimeError("index_document_verification_failed")
        return uris

    @safe_write
    def canary(self, root, query, expected_uris):
        payload = self.client.find(
            SELECTED_PROFILE.query_text(query),
            target_uri=root,
            limit=10,
            options={"context_type": "resource", "level": 2, "read_content": False},
        )
        if not any(
            r.get("uri") in expected_uris and r.get("level") == 2
            for r in payload.get("resources", [])
        ):
            raise RuntimeError("index_canary_failed")
