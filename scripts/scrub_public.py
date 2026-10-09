#!/usr/bin/env python3
"""Redact environment-identifying data before a public push.

What gets removed, and why:

  * resolved filesystem paths that embed a storage mount UUID — these identify
    the host and its storage layout
  * sandbox session identifiers used as object keys
  * anything that looks like a physical NAS/network mount path

What is deliberately KEPT:

  * the capability findings themselves (podman absent, systemd absent,
    PostgreSQL 16.2 present) — the findings are the deliverable; the machine
    that produced them is not
  * synthetic UUIDs used as fixtures — they are constants in the tests
  * command text, since a reviewer must be able to re-run the check

Idempotent. Run it over the tree, or as a git filter-branch tree-filter.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Physical mount paths: <redacted-mount-path>, alinas-*.us-east-1.tls..., etc.
MOUNT_PATH = re.compile(
    r"(?:<redacted-mount-path>)"
    r"[^\s\"',)]*"
)
# Sandbox session id, used as a dict key or value
SESSION_KEY = re.compile(r"\b(root_)?session_\d{6,}\b")
# "<uuid>.<uuid>.us-east-1..." storage endpoints
ENDPOINT = re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.[a-z]{2}-[a-z]+\d\b[^\s\"',)]*")

TEXT_SUFFIXES = {".json", ".md", ".py", ".sql", ".toml", ".txt", ".yml", ".yaml"}


def redact_text(text: str) -> str:
    text = MOUNT_PATH.sub("<redacted-mount-path>", text)
    text = ENDPOINT.sub("<redacted-endpoint>", text)
    text = SESSION_KEY.sub("session_<redacted>", text)
    return text


def redact_tree(root: pathlib.Path) -> list[str]:
    changed: list[str] = []
    skip = {".git", ".venv", "node_modules", "dist", ".tools", "__pycache__", "docs/handoff-input"}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in skip for part in rel.parts):
            continue
        if p.suffix not in TEXT_SUFFIXES:
            continue
        try:
            original = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        redacted = redact_text(original)

        # Only re-serialise JSON when redaction actually changed something.
        # Reformatting a file that needed no redaction is churn a reviewer
        # has to read for nothing.
        if p.suffix == ".json" and redacted != original:
            try:
                doc = json.loads(redacted)
            except json.JSONDecodeError:
                pass
            else:
                for key in ("workspace_absolute_resolved", "workspace_absolute_note"):
                    if isinstance(doc, dict) and key in doc:
                        del doc[key]
                for key in [
                    k for k in (doc if isinstance(doc, dict) else {}) if SESSION_KEY.fullmatch(k)
                ]:
                    if isinstance(doc, dict):
                        doc[key] = "cloud-session"
                redacted = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"

        if redacted != original:
            p.write_text(redacted, encoding="utf-8")
            changed.append(str(rel))
    return changed


def main() -> int:
    root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    changed = redact_tree(root)
    if changed:
        print(f"redacted {len(changed)} file(s):")
        for c in changed:
            print(f"  {c}")
    else:
        print("nothing to redact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
