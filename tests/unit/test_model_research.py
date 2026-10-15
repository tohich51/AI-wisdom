"""Support validation and shared slot release, without paid provider calls."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kb.adapters import model_runner
from kb.research.validation import validate_draft


def payload(kind="research"):
    ident, source = str(uuid4()), str(uuid4())
    fragments = [{"id": ident, "source_id": source, "text": "Измерьте результат эксперимента."}]
    item = {
        "heading": "Проверка",
        "text": fragments[0]["text"],
        "source_id": None,
        "parent_index": None,
        "citations": [{"fragment_id": ident, "quote": fragments[0]["text"]}],
    }
    draft = {"title": "Эксперимент", "no_evidence": False, "items": [item], "relations": []}
    request = {
        "task_id": str(uuid4()),
        "kind": kind,
        "query": "Как проверить?",
        "fragments": fragments,
    }
    return request, draft


def model_references(draft):
    """The model chooses references; exact source quotes belong to the application."""
    wire = deepcopy(draft)
    for item in wire["items"] + wire["relations"]:
        for citation in item["citations"]:
            del citation["quote"]
    return wire


def test_rejects_quote_and_topic_cycle():
    req, draft = payload()
    draft["items"][0]["citations"][0]["quote"] = "Несуществующая цитата"
    with pytest.raises(ValueError):
        validate_draft(draft, req["fragments"], "research")
    req, draft = payload("topic")
    draft["items"][0]["parent_index"] = 0
    with pytest.raises(ValueError):
        validate_draft(draft, req["fragments"], "topic")


def test_comparison_no_evidence_and_source_coverage():
    req, draft = payload("comparison")
    req["fragments"].append(
        {"id": str(uuid4()), "source_id": str(uuid4()), "text": "Другой источник."}
    )
    validate_draft(
        {"title": "Нет", "no_evidence": True, "items": [], "relations": []},
        req["fragments"],
        "comparison",
    )
    with pytest.raises(ValueError):
        validate_draft(draft, req["fragments"], "comparison")


@pytest.mark.parametrize("supported", [True, False])
def test_verification_and_slot_release(monkeypatch, supported):
    req, draft = payload()
    if not supported:
        req["fragments"][0]["text"] = "Unrelated evidence."
    answers = iter(
        [
            model_references(draft),
            {"items": [{"index": 0, "supported": supported}], "relations": []},
        ]
    )

    def cli(*args, **kwargs):
        return json.dumps(
            {
                "model": model_runner._MODEL,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(next(answers))}],
            }
        ).encode()

    monkeypatch.setenv("KB_MODEL_RUNNER_TOKEN", "a" * 32)
    monkeypatch.setattr(model_runner, "_run_cli", cli)
    with TestClient(model_runner.create_app()) as client:
        res = client.post("/research", json=req, headers={"X-KB-Worker-Token": "a" * 32})
    assert res.status_code == 200, res.text
    assert res.json()["support_verified"] is supported
    assert res.json()["no_evidence"] is (not supported)
    assert res.json()["provider_calls"] == 2
    if supported:
        assert res.json()["items"][0]["citations"][0]["quote"] == req["fragments"][0]["text"]
    assert model_runner._MODEL_SLOT.acquire(blocking=False)
    model_runner._MODEL_SLOT.release()


def test_malformed_verifier_auth_and_busy(monkeypatch):
    req, draft = payload()
    answers = iter([model_references(draft), {"items": [], "relations": []}])

    def cli(*args, **kwargs):
        return json.dumps(
            {
                "model": model_runner._MODEL,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(next(answers))}],
            }
        ).encode()

    monkeypatch.setenv("KB_MODEL_RUNNER_TOKEN", "a" * 32)
    monkeypatch.setattr(model_runner, "_run_cli", cli)
    with TestClient(model_runner.create_app()) as client:
        assert client.post("/research", json=req).status_code == 401
        res = client.post("/research", json=req, headers={"X-KB-Worker-Token": "a" * 32})
        assert res.status_code == 503
        assert model_runner._MODEL_SLOT.acquire(blocking=False)
        try:
            assert (
                client.post("/research", json=req, headers={"X-KB-Worker-Token": "a" * 32}).json()[
                    "detail"
                ]
                == "busy"
            )
        finally:
            model_runner._MODEL_SLOT.release()


@pytest.mark.parametrize(
    "stop,thinking,error,valid",
    [
        ("end_turn", True, False, True),
        ("max_tokens", True, False, False),
        ("end_turn", False, False, False),
        ("end_turn", True, True, False),
    ],
)
def test_runtime_stream_requires_independent_complete_protocol(
    monkeypatch, stop, thinking, error, valid
):
    proof = {
        "model": model_runner._MODEL,
        "stop_reason": stop,
        "message_stop": True,
        "thinking_seen": thinking,
        "error": error,
    }

    class Process:
        returncode = 0

        def communicate(self, timeout):
            return json.dumps({"content": '{"knowledge":[],"rules":[]}'}).encode(), (
                "MMX_STREAM_PROOF=" + json.dumps(proof)
            ).encode()

    monkeypatch.setattr(model_runner.subprocess, "Popen", lambda *args, **kwargs: Process())
    if valid:
        raw = model_runner._run_cli("Synthetic text", [uuid4()], "/synthetic/launcher", stream=True)
        assert model_runner._text_from_provider_output(raw) == '{"knowledge":[],"rules":[]}'
    else:
        with pytest.raises(model_runner._RunnerFailure):
            model_runner._run_cli("Synthetic text", [uuid4()], "/synthetic/launcher", stream=True)


def test_long_unicode_excerpt_is_canonical_and_verifier_gets_original(monkeypatch):
    req, draft = payload()
    original = ("Première ligne.\nDeuxième\u00a0ligne. " * 60)[:1800]
    req["fragments"][0]["text"] = original
    draft["items"][0]["text"] = "Первая строка и вторая строка."
    calls = []

    def cli(text, *args, **kwargs):
        calls.append(json.loads(text))
        answer = (
            model_references(draft)
            if len(calls) == 1
            else {"items": [{"index": 0, "supported": True}], "relations": []}
        )
        return json.dumps(
            {
                "model": model_runner._MODEL,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(answer)}],
            }
        ).encode()

    monkeypatch.setenv("KB_MODEL_RUNNER_TOKEN", "a" * 32)
    monkeypatch.setattr(model_runner, "_run_cli", cli)
    with TestClient(model_runner.create_app()) as client:
        res = client.post("/research", json=req, headers={"X-KB-Worker-Token": "a" * 32})
    assert res.status_code == 200, res.text
    assert res.json()["items"][0]["citations"][0]["quote"] == original
    assert calls[1]["draft"]["items"][0]["citations"][0]["quote"] == original
    assert "fragments" not in calls[1]


@pytest.mark.parametrize("invalid", ["unknown_id", "supplied_quote"])
def test_invalid_model_reference_never_reaches_verifier(monkeypatch, invalid):
    req, draft = payload()
    wire = model_references(draft)
    ref = wire["items"][0]["citations"][0]
    if invalid == "unknown_id":
        ref["fragment_id"] = str(uuid4())
    else:
        ref["quote"] = "Fabricated quote"
    calls = []

    def cli(*args, **kwargs):
        calls.append(True)
        return json.dumps(
            {
                "model": model_runner._MODEL,
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": json.dumps(wire)}],
            }
        ).encode()

    monkeypatch.setenv("KB_MODEL_RUNNER_TOKEN", "a" * 32)
    monkeypatch.setattr(model_runner, "_run_cli", cli)
    with TestClient(model_runner.create_app()) as client:
        res = client.post("/research", json=req, headers={"X-KB-Worker-Token": "a" * 32})
    assert res.status_code == 503 and res.json()["detail"] == "invalid_output"
    assert len(calls) == 1
    assert model_runner._MODEL_SLOT.acquire(blocking=False)
    model_runner._MODEL_SLOT.release()
