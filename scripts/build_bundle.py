#!/usr/bin/env python3
"""Build the source delivery bundle.

Packages source only. It never pushes, never deploys, never opens a public
listener, and never contacts the owner's server.

Excluded by policy: virtualenvs, caches, runtime databases, secrets, private
keys, and any real book content.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUNDLE = DIST / "knowledge-hub-source.zip"

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tools",
    "dist",
    "build",
    "var",
    ".var",
    "docs/handoff-input",
    ".skills",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".sqlite", ".sqlite3", ".log", ".key", ".pem"}
EXCLUDE_NAMES = {".env", ".DS_Store"}
# Never ship these even if they appear in the tree.
FORBIDDEN = ("id_rsa", "id_ed25519", ".pem", ".key", "credentials", "cookies")


def excluded(rel: str) -> bool:
    parts = pathlib.PurePosixPath(rel).parts
    if any(f"{a}/{b}" in EXCLUDE_DIRS for a in parts for b in parts):
        return True
    for p in parts:
        if p in {
            ".git",
            ".venv",
            "__pycache__",
            "node_modules",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".tools",
            "dist",
            "build",
            "var",
            ".var",
        }:
            return True
        if p in EXCLUDE_NAMES:
            return True
        if pathlib.Path(p).suffix in EXCLUDE_SUFFIX:
            return True
        if any(f in p.lower() for f in FORBIDDEN):
            return True
    if rel in {"docs/handoff-input/HANDOFF-MANIFEST.json"}:
        return True
    return rel.startswith("docs/handoff-input/")


def tracked_files() -> list[tuple[pathlib.Path, str]]:
    """Use this checkout's index, never filesystem discovery, as the allowlist."""
    git = shutil.which("git")
    if not git:
        raise ValueError("Git is required for source packaging")
    # Ignore Git routing overrides: only the repository containing ROOT is valid.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    def run(*args: str) -> bytes:
        return subprocess.run(
            [git, "-C", str(ROOT), *args],
            env=env,
            check=True,
            capture_output=True,
            timeout=20,
        ).stdout

    top = os.fsdecode(run("rev-parse", "--show-toplevel")).rstrip("\n")
    if pathlib.Path(top).resolve() != ROOT.resolve():
        raise ValueError("source root must be the exact Git worktree root")
    raw = run("ls-files", "--cached", "-z")
    if raw and not raw.endswith(b"\0"):
        raise ValueError("invalid Git file list")
    files = []
    for name in sorted(set(raw.split(b"\0")[:-1])):
        rel = os.fsdecode(name)
        parts = rel.split("/")
        if not rel or "\\" in rel or any(p in {"", ".", ".."} for p in parts):
            raise ValueError("invalid tracked relative path")
        if pathlib.PurePosixPath(rel).is_absolute():
            raise ValueError("absolute tracked path")
        path = ROOT
        for index, part in enumerate(parts):
            path = path / part
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError("tracked symlink is not permitted")
            if index < len(parts) - 1:
                if not stat.S_ISDIR(mode):
                    raise ValueError("invalid tracked parent")
            elif not stat.S_ISREG(mode):
                raise ValueError("tracked entry is not a regular file")
        files.append((path, rel))
    return files


def main() -> int:
    try:
        candidates = tracked_files()
    except (OSError, ValueError, subprocess.SubprocessError):
        print("REFUSING TO PACKAGE — invalid Git source tree", file=sys.stderr)
        return 1
    DIST.mkdir(exist_ok=True)
    files: list[tuple[pathlib.Path, str]] = []
    leaks: list[str] = []

    for p, rel in candidates:
        if excluded(rel):
            continue
        # Defensive second pass: flag rather than ship anything that smells.
        low = rel.lower()
        if any(f in low for f in FORBIDDEN) and not low.endswith(".example"):
            leaks.append(rel)
        files.append((p, rel))

    if leaks:
        print("REFUSING TO PACKAGE — possible secret material:", file=sys.stderr)
        for name in leaks:
            print(f"  {name}", file=sys.stderr)
        return 1

    if BUNDLE.exists():
        BUNDLE.unlink()
    with zipfile.ZipFile(BUNDLE, "w", zipfile.ZIP_DEFLATED) as z:
        for src, rel in files:
            z.write(src, f"knowledge-hub/{rel}")

    h = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "1.0",
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "bundle": BUNDLE.name,
        "bytes": BUNDLE.stat().st_size,
        "sha256": h,
        "file_count": len(files),
        "source_manifest_sha256": tree_digest(files),
        "git_sha": git_sha(),
        "note": "source only; not deployed, not production-ready",
    }
    (DIST / "source-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (DIST / "SHA256SUMS").write_text(f"{h}  {BUNDLE.name}\n", encoding="utf-8")

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print("\nwrote dist/knowledge-hub-source.zip + dist/source-manifest.json + dist/SHA256SUMS")
    print("packaging only — nothing was pushed, deployed or published")
    return 0


def tree_digest(files: list[tuple[pathlib.Path, str]]) -> str:
    d = hashlib.sha256()
    for src, rel in files:
        d.update(rel.encode())
        d.update(hashlib.sha256(src.read_bytes()).digest())
    return d.hexdigest()


def git_sha() -> str | None:
    git = shutil.which("git") or "git"
    try:
        r = subprocess.run(
            [git, "-C", str(ROOT), "rev-parse", "HEAD"],
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
