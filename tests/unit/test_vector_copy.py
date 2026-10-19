import json
from types import SimpleNamespace
from typing import ClassVar
from uuid import UUID

import httpx
import pytest
from openviking_sdk.errors import UnavailableError

from kb.adapters import vector_writer
from kb.retrieval.embedding_profile import SELECTED_PROFILE

URL = "http://synthetic.invalid"
KEY = "synthetic-index-key"
PROFILE = f"p-{SELECTED_PROFILE.profile_hash}"
SOURCE_ROOT = f"viking://resources/index/{PROFILE}/g-1"
TARGET_ROOT = f"viking://resources/index/{PROFILE}/g-2"
DOCUMENT_ID = UUID("12345678-1234-5678-1234-567812345678")
TEXT = "synthetic immutable source document"
SOURCE_URI = f"{SOURCE_ROOT}/{DOCUMENT_ID}.md"
TARGET_URI = f"{TARGET_ROOT}/{DOCUMENT_ID}.md"
PAIR = (DOCUMENT_ID, TEXT)
ACK = {
    "status": "ok",
    "result": {
        "operation": "copy",
        "phase": "completed",
        "from": SOURCE_URI,
        "to": TARGET_URI,
        "recursive": False,
        "vectors": {"scanned": 1, "written": 1, "deleted": 0, "restored": 0, "batches": 1},
    },
}


class FakeSDKClient:
    created: ClassVar[list] = []
    read_text = TEXT
    events = None

    def __init__(self, *, url, api_key, timeout):
        self.closed = False
        self.read_uris = []
        self.write_calls = 0
        self.created.append(self)

    def initialize(self):
        pass

    def wait_processed(self, timeout):
        return {"jobs": [{"diagnostics": {"error_count": 0}}]}

    def write(self, *args, **kwargs):
        self.write_calls += 1
        raise AssertionError("copy_document must not use the embedding SDK write")

    def read(self, uri):
        self.read_uris.append(uri)
        self.events.append("read")
        if isinstance(self.read_text, BaseException):
            raise self.read_text
        return self.read_text

    def close(self):
        self.closed = True


def assert_copy_attempt(harness):
    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert request.method == "POST"
    assert str(request.url) == URL + "/api/v1/fs/cp-vectors-only"
    assert json.loads(request.content) == {
        "from_uri": SOURCE_URI,
        "to_uri": TARGET_URI,
        "recursive": False,
        "processing_mode": "vectors_only",
    }
    assert request.headers.get("X-API-Key") == KEY
    assert not any("root" in name.lower() or "trust" in name.lower() for name in request.headers)


def assert_pending(harness):
    assert_copy_attempt(harness)
    assert harness.sdk.read_uris == []
    assert harness.sdk.write_calls == 0


def make_writer(monkeypatch, outcome, read_text=TEXT):
    requests, events, clients, constructor_kwargs = [], [], [], []
    FakeSDKClient.created = []
    FakeSDKClient.read_text = read_text
    FakeSDKClient.events = events

    real_client = httpx.Client  # Store this before patching the shared httpx module.

    def handler(request):
        requests.append(request)
        if isinstance(outcome, BaseException):
            raise outcome
        events.append("http")
        return outcome

    transport = httpx.MockTransport(handler)

    def client_factory(**kwargs):
        constructor_kwargs.append(kwargs)
        client = real_client(transport=transport, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(vector_writer.httpx, "Client", client_factory)
    monkeypatch.setattr(vector_writer, "SyncHTTPClient", FakeSDKClient)
    writer = vector_writer.OpenVikingWriter(URL, KEY)
    assert len(FakeSDKClient.created) == 1
    return writer, SimpleNamespace(
        sdk=FakeSDKClient.created[0],
        requests=requests,
        events=events,
        clients=clients,
        constructor_kwargs=constructor_kwargs,
    )


def test_copy_document_uses_native_cp_then_exact_read_and_closes_clients(monkeypatch):
    outcome = httpx.Response(200, json=ACK)
    writer, harness = make_writer(monkeypatch, outcome)
    assert writer.copy_document(TARGET_ROOT, SOURCE_ROOT, PAIR) == TARGET_URI
    assert_copy_attempt(harness)
    assert harness.events == ["http", "read"]
    assert harness.sdk.read_uris == [TARGET_URI]
    assert harness.sdk.write_calls == 0
    kwargs = harness.constructor_kwargs[0]
    headers = {name.lower(): value for name, value in kwargs["headers"].items()}
    assert headers.get("x-api-key") == KEY
    writer.close()
    assert harness.sdk.closed
    assert harness.clients[0].is_closed


@pytest.mark.parametrize(
    "target_root,source_root,pair",
    [
        pytest.param("not-a-uri", SOURCE_ROOT, PAIR, id="invalid-uri"),
        pytest.param(TARGET_ROOT, "viking://resources/index/p-invalid/g-1", PAIR, id="profile"),
        pytest.param(TARGET_ROOT, TARGET_ROOT, PAIR, id="same-generation"),
        pytest.param(TARGET_ROOT, SOURCE_ROOT, ("not-a-uuid", TEXT), id="invalid-uuid"),
    ],
)
def test_copy_rejects_invalid_identity_before_http(monkeypatch, target_root, source_root, pair):
    writer, harness = make_writer(monkeypatch, httpx.Response(200, json=ACK))
    with pytest.raises((ValueError, RuntimeError)) as caught:
        writer.copy_document(target_root, source_root, pair)
    assert not isinstance(caught.value, vector_writer.IndexCopyPending)
    assert harness.requests == []
    assert harness.sdk.read_uris == []
    assert harness.sdk.write_calls == 0


@pytest.mark.parametrize("status", [403, 404])
def test_copy_permission_or_unsupported_route_is_definite(monkeypatch, status):
    writer, harness = make_writer(monkeypatch, httpx.Response(status, json={"detail": "denied"}))
    with pytest.raises(RuntimeError) as caught:
        writer.copy_document(TARGET_ROOT, SOURCE_ROOT, PAIR)
    assert not isinstance(caught.value, vector_writer.IndexCopyPending)
    assert_pending(harness)


@pytest.mark.parametrize("status", [409, 429, 503])
def test_copy_transient_http_statuses_are_pending(monkeypatch, status):
    writer, harness = make_writer(monkeypatch, httpx.Response(status))
    with pytest.raises(vector_writer.IndexCopyPending):
        writer.copy_document(TARGET_ROOT, SOURCE_ROOT, PAIR)
    assert_pending(harness)


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(httpx.ReadTimeout("synthetic timeout"), id="timeout"),
        pytest.param(httpx.Response(200, content=b"not-json"), id="malformed"),
        pytest.param(
            httpx.Response(200, json={**ACK, "result": {**ACK["result"], "phase": "processing"}}),
            id="phase",
        ),
        pytest.param(
            httpx.Response(
                200,
                json={**ACK, "result": {**ACK["result"], "vectors": {"scanned": 1, "written": 0}}},
            ),
            id="count",
        ),
    ],
)
def test_copy_unknown_outcomes_are_pending_without_read(monkeypatch, outcome):
    writer, harness = make_writer(monkeypatch, outcome)
    with pytest.raises(vector_writer.IndexCopyPending):
        writer.copy_document(TARGET_ROOT, SOURCE_ROOT, PAIR)
    assert_pending(harness)


def test_copy_acknowledged_exact_read_mismatch_is_definite(monkeypatch):
    writer, harness = make_writer(
        monkeypatch, httpx.Response(200, json=ACK), read_text="different body"
    )
    with pytest.raises(RuntimeError) as caught:
        writer.copy_document(TARGET_ROOT, SOURCE_ROOT, PAIR)
    assert not isinstance(caught.value, vector_writer.IndexCopyPending)
    assert_copy_attempt(harness)
    assert harness.events == ["http", "read"]
    assert harness.sdk.read_uris == [TARGET_URI]
    assert harness.sdk.write_calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("deleted", None),
        ("deleted", 1),
        ("restored", None),
        ("restored", 1),
        ("batches", None),
        ("batches", 0),
    ],
)
def test_copy_incomplete_native_counters_remain_pending(monkeypatch, field, value):
    vectors = {**ACK["result"]["vectors"]}
    if value is None:
        vectors.pop(field)
    else:
        vectors[field] = value
    ack = {**ACK, "result": {**ACK["result"], "vectors": vectors}}
    writer, harness = make_writer(monkeypatch, httpx.Response(200, json=ack))
    try:
        with pytest.raises(vector_writer.IndexCopyPending):
            writer.copy_document(TARGET_ROOT, SOURCE_ROOT, PAIR)
        assert_pending(harness)
    finally:
        writer.close()


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("synthetic read timeout"), UnavailableError("synthetic unavailable")],
)
def test_copy_post_ack_transport_read_preserves_retry(monkeypatch, error):
    writer, harness = make_writer(monkeypatch, httpx.Response(200, json=ACK), read_text=error)
    try:
        with pytest.raises(vector_writer.IndexCopyPending):
            writer.copy_document(TARGET_ROOT, SOURCE_ROOT, PAIR)
        assert_copy_attempt(harness)
        assert harness.sdk.read_uris == [TARGET_URI]
        assert harness.sdk.write_calls == 0
    finally:
        writer.close()
