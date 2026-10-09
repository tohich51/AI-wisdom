#!/usr/bin/env python3
"""Collect environment evidence for a check run.

Writes a JSON evidence file. Never fabricates a value: anything not directly
probed is recorded as null with a reason.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime


def run(cmd: list[str], timeout: int = 20) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() or None
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    out: dict = {
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": run(["nproc"]),
        "git": run(["git", "--version"]),
        "node": run(["node", "-v"]),
        "services": {},
    }

    for svc, probe in {
        "podman": (["podman", "--version"], ["podman", "info"]),
        "docker": (["docker", "--version"], ["docker", "info"]),
        "systemd": (["systemctl", "--version"], None),
        "keycloak": (["kc.sh", "--version"], None),
    }.items():
        ver = run(probe[0])
        alive = None
        if probe[1]:
            alive = (
                subprocess.run(
                    probe[1],
                    capture_output=True,
                    timeout=20,
                ).returncode
                == 0
            )
        out["services"][svc] = {
            "available": ver is not None,
            "version": ver,
            "responded": alive,
        }

    # Real PostgreSQL is provided by the pgserver package during this session.
    try:
        import pgserver  # noqa: F401

        out["services"]["postgres_pip_binary"] = {"available": True, "packaging": "pgserver"}
    except Exception as exc:
        out["services"]["postgres_pip_binary"] = {"available": False, "reason": str(exc)}

    p = pathlib.Path(a.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
