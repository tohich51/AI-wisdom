"""Exact, verified vectors-only writes; exclusively used by the indexer."""

import re
from functools import wraps
from uuid import UUID

import httpx
from openviking_sdk import SyncHTTPClient
from openviking_sdk.errors import AlreadyExistsError, ConflictError

from kb.retrieval.embedding_profile import SELECTED_PROFILE

_INDEX_ROOT_PATTERN = (
    rf"viking://resources/index/p-{re.escape(SELECTED_PROFILE.profile_hash)}/g-[1-9][0-9]*"
)


class IndexCopyPending(RuntimeError):
    """The idempotent native copy may not have been acknowledged as completed."""


def safe_write(method):
    @wraps(method)
    def call(self, root, *args):
        if not re.fullmatch(_INDEX_ROOT_PATTERN, root):
            raise ValueError("invalid_index_root")
        try:
            return method(self, root, *args)
        except IndexCopyPending:
            raise
        except Exception:
            raise RuntimeError("index_writer_unavailable") from None

    return call


class OpenVikingWriter:
    def __init__(self, url, key):
        self.client = SyncHTTPClient(url=url, api_key=key, timeout=600)
        self.client.initialize()
        self._errors = self._error_count(self.client.wait_processed(timeout=600))
        self._copy_endpoint = f"{str(url).rstrip('/')}/api/v1/fs/cp"
        self._http = httpx.Client(
            headers={"X-API-Key": key},
            timeout=600,
        )

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
        try:
            self.client.close()
        finally:
            self._http.close()

    @safe_write
    def prepare(self, root):
        try:
            self.client.mkdir(root, description="Knowledge generation")
        except (AlreadyExistsError, ConflictError):
            pass
        self._wait()

    @safe_write
    def copy_document(self, root, source_root, document_pair):
        if (
            not isinstance(source_root, str)
            or re.fullmatch(_INDEX_ROOT_PATTERN, source_root) is None
            or source_root == root
        ):
            raise ValueError("invalid_index_copy_source")
        try:
            document_id, text = document_pair
        except (TypeError, ValueError):
            raise ValueError("invalid_index_document") from None
        if not isinstance(document_id, UUID) or not isinstance(text, str) or not text.strip():
            raise ValueError("invalid_index_document")

        source_uri = f"{source_root}/{document_id}.md"
        target_uri = f"{root}/{document_id}.md"
        try:
            response = self._http.post(
                self._copy_endpoint,
                json={
                    "from_uri": source_uri,
                    "to_uri": target_uri,
                    "recursive": False,
                    "processing_mode": "vectors_only",
                },
            )
        except httpx.RequestError:
            raise IndexCopyPending() from None

        if response.status_code in (409, 429) or 500 <= response.status_code < 600:
            raise IndexCopyPending()
        if not 200 <= response.status_code < 300:
            raise RuntimeError("index_copy_rejected")

        try:
            payload = response.json()
        except (TypeError, ValueError):
            raise IndexCopyPending() from None
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise IndexCopyPending()
        result = payload.get("result")
        if (
            not isinstance(result, dict)
            or result.get("operation") != "copy"
            or result.get("phase") != "completed"
            or result.get("from") != source_uri
            or result.get("to") != target_uri
            or result.get("recursive") is not False
        ):
            raise IndexCopyPending()

        vectors = result.get("vectors")
        if not isinstance(vectors, dict):
            raise IndexCopyPending()
        scanned, written = vectors.get("scanned"), vectors.get("written")
        if (
            type(scanned) is not int
            or type(written) is not int
            or scanned < 1
            or written != scanned
        ):
            raise IndexCopyPending()

        if self.client.read(target_uri) != text:
            raise RuntimeError("index_document_verification_failed")
        return target_uri

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
