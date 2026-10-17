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
        f'#!{sys.executable}\nimport json\nimport os\nfrom pathlib import Path\nimport sys\n\noptions = os.environ.get("NODE_OPTIONS", "").split()\nrequires = [\n    option.removeprefix("--require=")\n    for option in options\n    if option.startswith("--require=")\n]\nif len(requires) != 1:\n    raise SystemExit("missing NODE_OPTIONS preload")\npreload = Path(requires[0])\nexpected = Path(os.environ["KB_TEST_EXPECTED_PRELOAD"])\nif preload.name != "subscription-proof.cjs" or preload != expected:\n    raise SystemExit("wrong NODE_OPTIONS preload")\nif not preload.is_file():\n    raise SystemExit("NODE_OPTIONS preload does not exist")\nif "--stream" not in sys.argv:\n    raise SystemExit("stream flag missing")\nmodel = sys.argv[sys.argv.index("--model") + 1]\nproof = {{\n    "model": model,\n    "stop_reason": "end_turn",\n    "message_stop": True,\n    "error": False,\n    "thinking_seen": True,\n}}\nsys.stderr.write(\n    "MMX_STREAM_PROOF=" + json.dumps(proof, separators=(",", ":")) + "\\n"\n)\nsys.stdout.write(\n    json.dumps(\n        {{"content": "synthetic completion from local launcher"}},\n        separators=(",", ":"),\n    )\n    + "\\n"\n)\n',
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
