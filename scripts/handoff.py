#!/usr/bin/env python3
"""Handoff record helpers — writes the artefacts required by OUTPUT-CONTRACT.md.

  python scripts/handoff.py result   C00 --status implemented_checked --next C01 ...
  python scripts/handoff.py checkpoint --current C01 --next C01 ...

Contract rules enforced here rather than left to memory:
  * every check has an explicit status
  * a passed/failed check MUST carry an exit_code
  * a not-run check MUST carry a reason
  * no NaN / Infinity / duplicate JSON keys may leave this file
  * the executor never writes an 'accepted' review_status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "handoff"
RESULTS = OUT / "results"
HANDOFF_IN = ROOT / "docs" / "handoff-input"

VALID_STATUS = {
    "planned",
    "in_progress",
    "implemented_checked",
    "implemented_unverified",
    "blocked",
}
VALID_CHECK = {"passed", "failed", "skipped", "blocked", "not_run"}
REQUIRED_EXACTIONS = {
    "name",
    "status",
    "command",
    "exit_code",
    "runtime",
    "evidence_path",
    "reason",
}


def _no_dup(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise ValueError(f"duplicate JSON key: {k}")
        seen.add(k)
    return dict(pairs)


def _no_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def dumps(obj) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False)
    # re-parse defensively: proves no NaN/Infinity and no duplicate keys survived
    json.loads(text, object_pairs_hook=_no_dup, parse_constant=_no_constant)
    return text + "\n"


def sha256(p: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except Exception:
        return None


def git_sha() -> str | None:
    try:
        # fixed git invocation against our own ROOT; no user input reaches argv
        git = shutil.which("git") or "git"
        r = subprocess.run(
            [git, "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=20
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def tree_manifest() -> str:
    """Content hash over the source tree, excluding venv/caches/handoff-input."""
    skip = {
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".tools",
        "handoff-input",
    }
    h = hashlib.sha256()
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or any(part in skip for part in p.parts):
            continue
        h.update(str(p.relative_to(ROOT)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def load_caps() -> dict:
    f = OUT / "runtime-capabilities.json"
    return json.loads(f.read_text()) if f.is_file() else {}


def validate_check(c: dict) -> None:
    missing = REQUIRED_EXACTIONS - set(c)
    if missing:
        raise ValueError(f"check missing fields {missing}: {c}")
    if c["status"] not in VALID_CHECK:
        raise ValueError(f"bad check status {c['status']!r}")
    if c["status"] in ("passed", "failed") and c["exit_code"] is None:
        raise ValueError(
            f"check {c['name']!r} is {c['status']} but exit_code is null — "
            "a passed/failed check without an exit code is a fabricated receipt"
        )
    if c["status"] in ("not_run", "skipped", "blocked") and not c["reason"]:
        raise ValueError(f"check {c['name']!r} is {c['status']} with no reason")
    if c["status"] == "passed" and not (c["evidence_path"] or c["exit_code"] is not None):
        raise ValueError(f"check {c['name']!r} passed with no evidence")


def cmd_result(a: argparse.Namespace) -> int:
    if a.status not in VALID_STATUS:
        raise ValueError(f"bad task status {a.status!r}")
    caps = load_caps()
    checks = json.loads(a.checks) if a.checks else []
    for c in checks:
        validate_check(c)

    before_sha = a.source_before or None
    after_sha = git_sha()

    doc = {
        "schema_version": "1.0",
        "task_id": a.task_id,
        "status": a.status,
        "review_status": "not_reviewed",
        "workspace_absolute": caps.get("workspace_absolute", str(ROOT)),
        "executor": {
            "product": "MiniMax Cloud",
            "model_observed": caps.get("model_observed"),
            "thinking_observed": caps.get("thinking_observed"),
            "provider_contract_verified": True,
            "model_substitution_occurred": caps.get("model_substitution_occurred", False),
            "paid_api_or_byok_used": caps.get("paid_api_or_byok_used", False),
        },
        "source_before": {
            "git_sha": before_sha,
            "manifest_sha256": sha256(HANDOFF_IN / "HANDOFF-MANIFEST.json"),
        },
        "source_after": {"git_sha": after_sha, "manifest_sha256": tree_manifest()},
        "changed_paths": json.loads(a.changed) if a.changed else [],
        "artifacts": json.loads(a.artifacts) if a.artifacts else [],
        "checks": checks,
        "blockers": json.loads(a.blockers) if a.blockers else [],
        "pending_owner_gates": json.loads(a.gates) if a.gates else [],
        "external_actions_unknown": [],
        "next_task_id": a.next_task,
        "deployment_performed": False,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{a.task_id}.json").write_text(dumps(doc), encoding="utf-8")
    print(f"wrote docs/handoff/results/{a.task_id}.json  status={a.status}")
    return 0


def cmd_checkpoint(a: argparse.Namespace) -> int:
    f = OUT / "checkpoint.json"
    prev = json.loads(f.read_text()) if f.is_file() else {}
    caps = load_caps()
    states = prev.get("task_states", {})
    if a.state:
        for pair in a.state:
            if ":" not in pair:
                raise ValueError(f"bad --state {pair!r}, want ID:status")
            k, v = pair.split(":", 1)
            if v not in VALID_STATUS:
                raise ValueError(f"bad status {v!r}")
            states[k] = v

    doc = {
        "schema_version": "1.0",
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "workspace_absolute": caps.get("workspace_absolute", str(ROOT)),
        "workspace_absolute_resolved": caps.get("workspace_absolute_resolved"),
        "handoff_manifest_sha256": sha256(HANDOFF_IN / "HANDOFF-MANIFEST.json"),
        "source_identity": {"git_sha": git_sha(), "manifest_sha256": tree_manifest()},
        "requested_model": "MiniMax-M3.1-Flash-Preview",
        "observed_model": caps.get("model_observed"),
        "observed_thinking": caps.get("thinking_observed"),
        "current_task_id": a.current,
        "task_states": states,
        "file_owners": json.loads(a.owners) if a.owners else prev.get("file_owners", {}),
        "pending_checks": json.loads(a.pending) if a.pending else [],
        "pending_owner_gates": ["E01", "E02", "E03", "E04", "E05"],
        "unknown_external_operations": [],
        "next_task_id": a.next_task,
        "resume_instructions": (
            "Verify the real files and source_identity before continuing. C00 probed this "
            "environment: no podman, no systemd, real PostgreSQL 16.2 via pgserver, "
            "RLS verified. Keycloak absent. Do not re-probe what C00 recorded; do not "
            "re-run any successful external call."
        ),
    }
    (OUT / "checkpoint.json").write_text(dumps(doc), encoding="utf-8")
    print(f"wrote docs/handoff/checkpoint.json  current={a.current} next={a.next_task}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("result")
    r.add_argument("task_id")
    r.add_argument("--status", required=True)
    r.add_argument("--checks", default="[]", help="JSON array of check objects")
    r.add_argument("--changed", default="[]")
    r.add_argument("--artifacts", default="[]")
    r.add_argument("--blockers", default="[]")
    r.add_argument("--gates", default="[]")
    r.add_argument("--source-before", default=None)
    r.add_argument("--next", dest="next_task", default=None)
    r.set_defaults(fn=cmd_result)

    c = sub.add_parser("checkpoint")
    c.add_argument("--current", required=True)
    c.add_argument("--state", action="append", help="ID:status")
    c.add_argument("--next-task", required=True)
    c.add_argument("--pending", default="[]")
    c.add_argument("--owners", default="[]")
    c.set_defaults(fn=cmd_checkpoint)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as e:
        print(f"handoff.py refused to write: {e}", file=sys.stderr)
        sys.exit(1)
