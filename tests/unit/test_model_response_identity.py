"""Observed managed response names must not bypass the extraction boundary."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from kb.jobs import model_runner as runner

REQUESTED = "MiniMax-M3.1-Flash-Preview"
CANONICAL = "MiniMax-M3.1-Flash"


def extraction():
    fragment = str(uuid4())
    request = {"task_id": str(uuid4()), "text": "Проверьте форму.", "fragment_ids": [fragment]}
    draft = {
        "knowledge": [
            {
                "statement": "Форму следует проверять.",
                "kind": "recommendation",
                "fragment_ids": [fragment],
            }
        ],
        "rules": [
            {
                "title": "Проверяйте форму",
                "when_to_apply": "Перед выпуском формы",
                "inputs": [],
                "preconditions": [],
                "actions": ["Проверьте форму."],
                "exceptions": [],
                "expected_effect": None,
                "verification": "Форма проверена.",
                "fragment_ids": [fragment],
            }
        ],
    }
    envelope = {
        "model": CANONICAL,
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "Synthetic only"},
            {"type": "text", "text": json.dumps(draft)},
        ],
    }
    return request, draft, envelope


def generate(monkeypatch, request, raw):
    monkeypatch.setenv("KB_MODEL_RUNNER_TOKEN", "s" * 32)
    monkeypatch.setattr(runner, "_run_cli", lambda *args, **kwargs: raw)
    with TestClient(runner.create_app()) as client:
        result = client.post("/generate", json=request, headers={"X-KB-Worker-Token": "s" * 32})
    assert runner._MODEL_SLOT.acquire(blocking=False)
    runner._MODEL_SLOT.release()
    return result


@pytest.mark.parametrize("model", [REQUESTED, CANONICAL])
def test_native_extraction_accepts_complete_observed_identity(monkeypatch, model):
    request, draft, envelope = extraction()
    envelope["model"] = model
    response = generate(monkeypatch, request, json.dumps(envelope).encode())
    assert response.status_code == 200, response.text
    assert response.json() == {"model": REQUESTED, "task_id": request["task_id"], **draft}


@pytest.mark.parametrize(
    "model",
    [None, False, "MiniMax-M3", "other/model", CANONICAL + "-other", REQUESTED + "-other"],
)
def test_native_extraction_refuses_other_response_identities(monkeypatch, model):
    request, _, envelope = extraction()
    envelope["model"] = model
    response = generate(monkeypatch, request, json.dumps(envelope).encode())
    assert response.status_code == 503
    assert response.json() == {"detail": "invalid_output"}


@pytest.mark.parametrize("observed,accepted", [(CANONICAL, False), ("synthetic/configured", True)])
def test_nonstream_identity_is_bound_to_the_configured_request(monkeypatch, observed, accepted):
    monkeypatch.setattr(runner, "_MODEL", "synthetic/configured")
    _, _, envelope = extraction()
    envelope["model"] = observed
    raw = json.dumps(envelope).encode()
    if accepted:
        assert json.loads(runner._text_from_provider_output(raw))["knowledge"]
    else:
        with pytest.raises(runner._RunnerFailure, match="invalid_output"):
            runner._text_from_provider_output(raw)


@pytest.mark.parametrize(
    "failure",
    [
        "missing_model",
        "truncated",
        "thinking_only",
        "empty_text",
        "malformed",
        "foreign_fragment",
        "provider_error",
    ],
)
def test_canonical_identity_preserves_output_and_provenance_guards(monkeypatch, failure):
    request, draft, envelope = extraction()
    if failure == "missing_model":
        del envelope["model"]
    elif failure == "truncated":
        envelope["stop_reason"] = "max_tokens"
    elif failure == "thinking_only":
        envelope["content"] = envelope["content"][:1]
    elif failure == "empty_text":
        envelope["content"][-1]["text"] = ""
    elif failure == "malformed":
        envelope["content"][-1]["text"] = "not JSON"
    elif failure == "foreign_fragment":
        draft["rules"][0]["fragment_ids"] = [str(uuid4())]
        envelope["content"][-1]["text"] = json.dumps(draft)
    elif failure == "provider_error":
        envelope["base_resp"] = {"status_code": 1000}
    response = generate(monkeypatch, request, json.dumps(envelope).encode())
    assert response.status_code == 503


@pytest.mark.parametrize(
    "configured,observed,mutation,accepted",
    [
        (REQUESTED, REQUESTED, {}, True),
        (REQUESTED, CANONICAL, {}, True),
        (REQUESTED, None, {}, False),
        (REQUESTED, "other/model", {}, False),
        (REQUESTED, CANONICAL, {"thinking_seen": False}, False),
        (REQUESTED, CANONICAL, {"message_stop": False}, False),
        (REQUESTED, CANONICAL, {"stop_reason": "max_tokens"}, False),
        (REQUESTED, CANONICAL, {"error": True}, False),
        ("synthetic/configured", CANONICAL, {}, False),
        ("synthetic/configured", "synthetic/configured", {}, True),
    ],
)
def test_stream_identity_keeps_independent_completion_proof(
    monkeypatch, configured, observed, mutation, accepted
):
    monkeypatch.setattr(runner, "_MODEL", configured)
    proof = {
        "model": observed,
        "stop_reason": "end_turn",
        "message_stop": True,
        "thinking_seen": True,
        "error": False,
        **deepcopy(mutation),
    }
    launches = []

    class Process:
        returncode = 0
        stdout = stderr = None

    def launch(args, **kwargs):
        launches.append(args)
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(
        runner,
        "_capture_bounded",
        lambda process, timeout: (
            json.dumps({"content": '{"knowledge":[],"rules":[]}'}).encode(),
            ("MMX_STREAM_PROOF=" + json.dumps(proof)).encode(),
        ),
    )
    if accepted:
        raw = runner._run_cli("Synthetic source", [uuid4()], "/synthetic/launcher", stream=True)
        assert json.loads(raw)["model"] == configured
        assert runner._text_from_provider_output(raw) == '{"knowledge":[],"rules":[]}'
    else:
        with pytest.raises(runner._RunnerFailure, match="incomplete_output"):
            runner._run_cli("Synthetic source", [uuid4()], "/synthetic/launcher", stream=True)
    assert launches[0][launches[0].index("--model") + 1] == configured
