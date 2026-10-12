from uuid import uuid4

import pytest

from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.secret_store import FileSecretSink
from kb.retrieval.vector_reader import IndexUnavailable, OpenVikingReader

URL = "https://vector.invalid"
KEY = "synthetic-read-key"
ROOT = f"viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-1"
OPTIONS = {"context_type": "resource", "level": 2, "read_content": False}


class FakeClient:
    def __init__(self, resources=(), error=None):
        self.resources = resources
        self.error = error
        self.initialized = self.closed = False
        self.find_calls = []

    def initialize(self):
        self.initialized = True

    def find(self, query, **kwargs):
        self.find_calls.append((query, kwargs))
        if self.error is not None:
            raise RuntimeError(self.error)
        return {"resources": self.resources}

    def close(self):
        self.closed = True


def reader_for(tmp_path, identity, client):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return client

    reader = OpenVikingReader(URL, tmp_path / "store" / "read", factory)
    return reader, calls


@pytest.fixture
def read_identity(tmp_path):
    identity = f"kb-svc-read-{uuid4().hex}"
    FileSecretSink(tmp_path / "store").write(identity, KEY)
    return identity


def test_query_uses_profile_and_closes_client(tmp_path, read_identity):
    identity = read_identity
    client = FakeClient([{"uri": ROOT + "/kept", "score": 0.8, "level": 2}])
    reader, calls = reader_for(tmp_path, identity, client)
    expected_query = SELECTED_PROFILE.query_text("synthetic query")
    result = reader.find(identity, ROOT, "synthetic query", 7)
    assert [(item.uri, item.score) for item in result] == [(ROOT + "/kept", 0.8)]
    assert expected_query.startswith("Instruct:")
    assert calls == [{"url": URL, "api_key": KEY, "timeout": 45}]
    assert client.find_calls == [
        (expected_query, {"target_uri": ROOT, "limit": 7, "options": OPTIONS}),
    ]
    assert client.initialized and client.closed


def test_filters_unsafe_resources(tmp_path, read_identity):
    resources = [
        {"uri": ROOT + "/kept", "score": 0.8, "level": 2},
        {"uri": ROOT + "/nan", "score": float("nan")},
        {"uri": ROOT + "/bool", "score": True},
        {"uri": "viking://resources/index/p-foreign/g-1/item", "score": 0.9},
        {"uri": ROOT + "/wrong-level", "score": 0.7, "level": 1},
    ]
    client = FakeClient(resources)
    reader, _ = reader_for(tmp_path, read_identity, client)
    result = reader.find(read_identity, ROOT, "synthetic query", 10)
    assert [(item.uri, item.score) for item in result] == [(ROOT + "/kept", 0.8)]


def test_sdk_failure_is_sanitized_and_client_closes(tmp_path, read_identity):
    identity = read_identity
    marker = "synthetic-private-value"
    client = FakeClient(error=f"synthetic SDK failure: {marker}")
    reader, _ = reader_for(tmp_path, identity, client)
    with pytest.raises(IndexUnavailable) as raised:
        reader.find(identity, ROOT, "synthetic query", 5)
    assert type(raised.value) is IndexUnavailable
    assert raised.value.args == ("index_unavailable",)
    assert marker not in str(raised.value)
    assert client.closed


def test_index_identity_rejected_before_client_creation(tmp_path):
    identity = f"kb-svc-index-{uuid4().hex}"
    client = FakeClient()
    reader, calls = reader_for(tmp_path, identity, client)
    with pytest.raises(IndexUnavailable) as raised:
        reader.find(identity, ROOT, "synthetic query", 5)
    assert raised.value.args == ("index_unavailable",)
    assert calls == []
