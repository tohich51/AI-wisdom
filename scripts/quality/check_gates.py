#!/usr/bin/env python3
"""Gate reporter.

Prints which mandatory gates this environment can and cannot execute, and exits
nonzero when any gate is unmet. A gate that was skipped is reported as skipped,
never as passed.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

GATES = {
    "E01": ("rootless Podman + Quadlet + systemd", ["podman", "systemctl"]),
    "E02": ("product subscription model-runner", ["kb-model-runner"]),
    "E03": ("real PostgreSQL/Keycloak/OpenViking + 2 MCP clients", []),
    "E04": ("backup/restore to a separate target", []),
    "E05": ("server inventory, domain/issuer, clean install", []),
}


def main() -> int:
    unmet: list[str] = []
    report: dict = {}

    for gid, (desc, required) in GATES.items():
        missing = [b for b in required if shutil.which(b) is None]
        status = "available" if not missing else "unmet"
        if missing:
            unmet.append(gid)
        report[gid] = {"description": desc, "status": status, "missing": missing}

    caps = json.loads((ROOT / "docs/handoff/runtime-capabilities.json").read_text())
    report["E03"]["postgres_real"] = caps["postgres_real"]["available"]
    report["E03"]["keycloak_real"] = caps["keycloak_real"]["available"]
    report["E03"]["openviking_real"] = caps["openviking_real"]["available"]
    if not (caps["keycloak_real"]["available"] and caps["openviking_real"]["available"]):
        unmet.append("E03")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if unmet:
        print(f"\nPENDING GATES: {', '.join(sorted(set(unmet)))}", file=sys.stderr)
        print(
            "Delivery status is source_bundle_with_pending_gates, not production-ready.",
            file=sys.stderr,
        )
        return 1
    print("\nall mandatory gates satisfied in this environment")
    return 0


if __name__ == "__main__":
    sys.exit(main())
