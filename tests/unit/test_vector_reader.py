from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from kb.adapters.secrets import FileSecretSink
from kb.adapters.vector_reader import Candidate, FrozenCandidate, IndexUnavailable, OpenVikingReader
from kb.retrieval.embedding_profile import SELECTED_PROFILE

URL = "https://vector.invalid"
KEY = "synthetic-read-key"
ROOT = f"viking://resources/index/p-{SELECTED_PROFILE.profile_hash}/g-1"
OPTIONS = {"context_type": "resource", "level": 2, "read_content": False}


class FakeClient:
    def __init__(self, resources=(), error=None):
        # A real SDK response carries a list; a tuple here would be refused by
        # the payload guard and hide which of the two actually failed.
        self.resources = list(resources)
        self.error = error
        self.initialized = self.closed = False
        self.find_calls = []
        self.closes = 0

    def initialize(self):
        self.initialized = True

    def find(self, query, **kwargs):
        self.find_calls.append((query, kwargs))
        if self.error is not None:
            raise RuntimeError(self.error)
        return {"resources": self.resources}

    def close(self):
        self.closed = True
        self.closes += 1


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


def test_query_uses_profile_and_the_client_is_bounded(tmp_path, read_identity):
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
    assert client.initialized and not client.closed

    # The client is pooled, so a second query is a second call on the same
    # client rather than a second TLS handshake and initialize().
    reader.find(identity, ROOT, "synthetic query", 7)
    assert len(calls) == 1
    assert len(client.find_calls) == 2
    assert client.closes == 0

    reader.close()
    assert client.closed


def test_filtered_resources_are_still_filtered_on_a_shared_client(tmp_path, read_identity):
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
    reader.close()


def test_sdk_failure_is_sanitized_and_the_client_is_released(tmp_path, read_identity):
    identity = read_identity
    marker = "synthetic-private-value"
    client = FakeClient(error=f"synthetic SDK failure: {marker}")
    reader, _ = reader_for(tmp_path, identity, client)
    with pytest.raises(IndexUnavailable) as raised:
        reader.find(identity, ROOT, "synthetic query", 5)
    assert type(raised.value) is IndexUnavailable
    assert raised.value.args == ("index_unavailable",)
    assert marker not in str(raised.value)

    # A failed call does not poison the pool: the client is handed back, not
    # dropped, and the next query still works.
    client.error = None
    assert reader.find(identity, ROOT, "synthetic query", 5) == []
    reader.close()
    assert client.closed


def test_index_identity_rejected_before_client_creation(tmp_path):
    identity = f"kb-svc-index-{uuid4().hex}"
    client = FakeClient()
    reader, calls = reader_for(tmp_path, identity, client)
    with pytest.raises(IndexUnavailable) as raised:
        reader.find(identity, ROOT, "synthetic query", 5)
    assert raised.value.args == ("index_unavailable",)
    assert calls == []


def test_invalid_root_or_limit_is_a_caller_error_not_an_outage(tmp_path, read_identity):
    identity = read_identity
    client = FakeClient()
    reader, calls = reader_for(tmp_path, identity, client)

    with pytest.raises(ValueError):
        reader.find(identity, "viking://resources/index/p-foreign/g-1", "q", 5)
    with pytest.raises(ValueError):
        reader.find(identity, ROOT + "/extra", "q", 5)
    with pytest.raises(ValueError):
        reader.find(identity, ROOT, "q", 0)
    with pytest.raises(ValueError):
        reader.find(identity, ROOT, "q", 101)
    with pytest.raises(ValueError):
        reader.find(identity, ROOT, "q", True)

    # A rejected argument must not have reached the network.
    assert calls == []
    assert client.find_calls == []
    reader.close()


def test_malformed_index_responses_are_refused(tmp_path, read_identity):
    identity = read_identity
    for payload in (None, [], {"resources": "not-a-list"}, {"resources": None}):
        # Bound as a default so the loop variable cannot leak between rounds.
        class Odd(FakeClient):
            def find(self, query, _payload=payload, **kwargs):
                return _payload

        client = Odd()
        reader, _ = reader_for(tmp_path, identity, client)
        with pytest.raises(IndexUnavailable):
            reader.find(identity, ROOT, "q", 5)
        reader.close()


def test_candidate_is_the_frozen_type_the_caller_can_reuse(tmp_path, read_identity):
    identity = read_identity
    client = FakeClient([{"uri": ROOT + "/kept", "score": 0.8, "level": 2}])
    reader, _ = reader_for(tmp_path, identity, client)
    result = reader.find(identity, ROOT, "q", 5)
    assert result == [Candidate(ROOT + "/kept", 0.8)]
    assert isinstance(result[0], FrozenCandidate)
    with pytest.raises(FrozenInstanceError):
        result[0].score = 0.1
    reader.close()
