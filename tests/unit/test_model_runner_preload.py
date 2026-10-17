from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from kb.jobs import model_runner


def test_stream_preload_uses_package_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_model = "synthetic/preload-regression"
    monkeypatch.setattr(model_runner, "_MODEL", synthetic_model)

    def as_data_payload(text: str) -> SimpleNamespace:
        return SimpleNamespace(framed_text=text)

    monkeypatch.setattr(model_runner.upload_data, "as_data_payload", as_data_payload)
    expected_preload = Path(model_runner.__file__).resolve().parents[1] / "subscription-proof.cjs"
    monkeypatch.setenv("KB_TEST_EXPECTED_PRELOAD", str(expected_preload))
    launcher = tmp_path / "synthetic-launcher"
    launcher.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

options = os.environ.get("NODE_OPTIONS", "").split()
requires = [
    option.removeprefix("--require=")
    for option in options
    if option.startswith("--require=")
]
if len(requires) != 1:
    raise SystemExit("missing NODE_OPTIONS preload")
preload = Path(requires[0])
expected = Path(os.environ["KB_TEST_EXPECTED_PRELOAD"])
if preload.name != "subscription-proof.cjs" or preload != expected:
    raise SystemExit("wrong NODE_OPTIONS preload")
if not preload.is_file():
    raise SystemExit("NODE_OPTIONS preload does not exist")
if "--stream" not in sys.argv:
    raise SystemExit("stream flag missing")
model = sys.argv[sys.argv.index("--model") + 1]
proof = {{
    "model": model,
    "stop_reason": "end_turn",
    "message_stop": True,
    "error": False,
    "thinking_seen": True,
}}
sys.stderr.write(
    "MMX_STREAM_PROOF=" + json.dumps(proof, separators=(",", ":")) + "\\n"
)
sys.stdout.write(
    json.dumps(
        {{"content": "synthetic completion from local launcher"}},
        separators=(",", ":"),
    )
    + "\\n"
)
""",
        encoding="utf-8",
    )
    launcher.chmod(448)
    result = model_runner._run_cli(
        "synthetic input",
        [UUID("00000000-0000-0000-0000-000000000001")],
        str(launcher),
        stream=True,
    )
    assert json.loads(result) == {
        "model": synthetic_model,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "synthetic completion from local launcher"}],
    }
