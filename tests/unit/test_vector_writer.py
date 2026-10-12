from uuid import uuid4

import pytest

from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.vector_writer import OpenVikingWriter

ROOT = f"viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-1"


class FakeClient:
    wait_error_counts = (0,)

    def __init__(self, *, url, api_key, timeout):
        self.calls = []
        self.stored = {}
        self.read_results = {}
        self.wait_index = 0
        self.consistency = {"ok": True, "missing_record_count": 0, "expected_count": 0}
        self.find_payload = {"resources": []}

    def initialize(self):
        self.calls.append(("initialize",))

    def wait_processed(self, timeout):
        index = min(self.wait_index, len(self.wait_error_counts) - 1)
        self.wait_index += 1
        self.calls.append(("wait_processed", timeout))
        return {"jobs": [{"diagnostics": {"error_count": self.wait_error_counts[index]}}]}

    def write(self, uri, text, wait, options):
        self.calls.append(("write", uri, text, wait, options))
        self.stored[uri] = text

    def read(self, uri):
        self.calls.append(("read", uri))
        return self.read_results.get(uri, self.stored.get(uri))

    def check_consistency(self, root):
        self.calls.append(("check_consistency", root))
        return self.consistency

    def find(self, query, *, target_uri, limit, options):
        self.calls.append(("find", query, target_uri, limit, options))
        return self.find_payload

    def close(self):
        self.calls.append(("close",))


def make_writer(monkeypatch):
    monkeypatch.setattr("kb.retrieval.vector_writer.SyncHTTPClient", FakeClient)
    return OpenVikingWriter("http://synthetic.invalid", "")


def test_write_batch_queues_vectors_only_then_waits_and_reads_each_document(monkeypatch):
    writer = make_writer(monkeypatch)
    client = writer.client
    client.calls.clear()
    documents = [(uuid4(), f"synthetic document {uuid4()}") for _ in range(2)]
    uris = writer.write_batch(ROOT, documents)
    expected_uris = [f"{ROOT}/{document_id}.md" for document_id, _ in documents]
    assert uris == expected_uris
    assert [call[0] for call in client.calls] == [
        "write",
        "write",
        "wait_processed",
        "read",
        "read",
    ]
    assert [call[1:3] for call in client.calls if call[0] == "write"] == [
        (uri, text) for uri, (_, text) in zip(expected_uris, documents, strict=True)
    ]
    assert all(
        call[3:] == (False, {"processing_mode": "vectors_only"})
        for call in client.calls
        if call[0] == "write"
    )
    assert [call[1] for call in client.calls if call[0] == "read"] == expected_uris


def test_write_batch_rejects_a_stored_document_text_mismatch(monkeypatch):
    writer = make_writer(monkeypatch)
    document_id, text = uuid4(), f"synthetic document {uuid4()}"
    uri = f"{ROOT}/{document_id}.md"
    writer.client.read_results[uri] = "different synthetic document"
    with pytest.raises(RuntimeError, match="index_writer_unavailable"):
        writer.write_batch(ROOT, [(document_id, text)])


def test_verify_refuses_missing_vector_records_even_when_text_exists(monkeypatch):
    writer = make_writer(monkeypatch)
    document_id, text = uuid4(), f"synthetic document {uuid4()}"
    uri = f"{ROOT}/{document_id}.md"
    writer.client.read_results[uri] = text
    writer.client.consistency.update(missing_record_count=1, expected_count=1)
    with pytest.raises(RuntimeError, match="index_writer_unavailable"):
        writer.verify(ROOT, [(document_id, text)])
    assert not any(call[0] == "read" for call in writer.client.calls)


def test_processing_error_increase_is_refused_but_initial_errors_are_allowed(monkeypatch):
    monkeypatch.setattr(FakeClient, "wait_error_counts", (2, 2, 3))
    writer = make_writer(monkeypatch)
    assert writer.write_batch(ROOT, []) == []
    with pytest.raises(RuntimeError, match="index_writer_unavailable"):
        writer.write_batch(ROOT, [])


def test_canary_requires_expected_level2_and_ignores_other_results(monkeypatch):
    writer = make_writer(monkeypatch)
    client = writer.client
    query = f"synthetic canary {uuid4()}"
    expected_uri = f"{ROOT}/{uuid4()}.md"
    level0_uri = f"{ROOT}/{uuid4()}.md"
    unrelated_uri = f"{ROOT}/{uuid4()}.md"
    distractors = [
        {"uri": level0_uri, "level": 0},
        {"uri": unrelated_uri, "level": 2},
    ]
    client.find_payload = {"resources": distractors}
    with pytest.raises(RuntimeError, match="index_writer_unavailable"):
        writer.canary(ROOT, query, (expected_uri, level0_uri))
    client.find_payload = {"resources": [*distractors, {"uri": expected_uri, "level": 2}]}
    assert writer.canary(ROOT, query, (expected_uri, level0_uri)) is None
    assert client.calls[-1] == (
        "find",
        SELECTED_PROFILE.query_text(query),
        ROOT,
        10,
        {"context_type": "resource", "level": 2, "read_content": False},
    )
