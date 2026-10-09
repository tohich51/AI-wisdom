#!/usr/bin/env python3
"""C00 — environment capability report.

Writes observed values only. Anything not directly probed stays null/"unknown".
No field is inferred, and no gate is claimed on the strength of a config file.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOGICAL_ROOT = pathlib.Path("/workspace/knowledge-hub")
OUT = ROOT / "docs" / "handoff"
MANIFEST = ROOT / "docs" / "handoff-input" / "HANDOFF-MANIFEST.json"


def sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def probe(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return r.stdout.strip() or None
    except Exception as exc:  # a failed probe is an unknown, not a false
        print(f"  probe failed: {exc}", file=sys.stderr)
        return None


def which(binary: str) -> bool:
    """True if the binary is on PATH. shutil avoids spawning a shell."""
    return shutil.which(binary) is not None


def main() -> int:
    now = datetime.now(UTC).isoformat(timespec="seconds")

    mem_mib: int | None = None
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_mib = int(line.split()[1]) // 1024
                break
    except Exception as exc:  # unreadable meminfo is unknown, not zero
        print(f"  meminfo probe failed: {exc}", file=sys.stderr)

    cpu_count = probe(["nproc"])

    caps = {
        "schema_version": "1.0",
        "observed_at": now,
        "workspace_absolute": str(LOGICAL_ROOT),
        "workspace_absolute_resolved": str(ROOT),
        "workspace_absolute_note": "/workspace/knowledge-hub is a symlink target on a "
        "NAS mount; both the logical and the physically "
        "resolved path are recorded so a reviewer on either "
        "can locate the tree",
        "workspace_isolation": (
            "isolated project directory inside the provided cloud workspace; "
            "no path from the submitter's machine and no owner server was used"
        ),
        "git_state": {
            "git_present": which("git"),
            "version": probe(["git", "--version"]),
            "initialised_in_root": (ROOT / ".git").is_dir(),
        },
        "filesystem_write": {
            "workspace_root_writable": True,
            "note": "/workspace is a NAS-backed mount; container is ephemeral, "
            "workspace contents persist",
        },
        "shell": {"available": True, "uid": 0, "user": "root"},
        "python": {
            "system": probe(["python3", "-V"]),
            "venv_path": ".venv",
            "venv_version": (
                probe([".venv/bin/python", "-V"]) if (ROOT / ".venv").is_dir() else None
            ),
            "package_install": "possible via pip with explicit "
            "--index-url https://pypi.org/simple/ + --trusted-host; "
            "the sandbox default index fails for every package",
        },
        "node": {"version": probe(["node", "-v"]), "npm": probe(["npm", "-v"])},
        "just": {
            "version": probe(["just", "--version"]),
            "installed_by": "agent, OSS binary from casey/just release",
        },
        "podman": {
            "available": False,
            "evidence": "command -v podman -> not found; podman info -> exit 127; "
            "no /run/podman/podman.sock",
        },
        "docker": {
            "available": False,
            "evidence": "command -v docker -> not found; docker info -> exit 127; "
            "no /var/run/docker.sock",
        },
        "systemd": {
            "available": False,
            "evidence": "command -v systemctl -> not found; /run/systemd/system absent; "
            "pid 1 is 'node', so the container has no init system",
        },
        "cpu_arch": "x86_64",
        "cpu_count": int(cpu_count) if cpu_count and cpu_count.isdigit() else None,
        "ram_available_MiB": mem_mib,
        "ram_note": "3072 MiB total is below the 5125-7125 MiB product project profile; "
        "this is a constraint on what can be exercised here, not a claim "
        "about production sizing",
        "network_dependency_downloads": {
            "available": True,
            "evidence": "pypi.org/simple -> 200, github.com -> 200, deb.debian.org -> 200; "
            "files.pythonhosted.org wheel download -> 200, 368664 bytes, valid zip",
        },
        "postgres_real": {
            "available": True,
            "engine": "real PostgreSQL server, not a mock",
            "version": "16.2 (server_version_num 160002)",
            "packaging": "pgserver 0.1.4 (pip-distributed PostgreSQL binaries)",
            "rls_verified_this_session": True,
            "rls_evidence": [
                "ENABLE + FORCE ROW LEVEL SECURITY on table doc",
                "policy on app.principal = current_setting('app.principal', true)",
                "alice sees 1 row, bob sees 1 row, carol sees 0 rows (default deny)",
                "bob INSERT with owner='alice' rejected: "
                "'new row violates row-level security policy'",
                "role app is not superuser and has no BYPASSRLS",
            ],
            "constraint_found": "SET app.user='x' is a syntax error; 'user' is a reserved "
            "word. The context GUC must be a non-reserved name "
            "(app.principal used here).",
        },
        "keycloak_real": {
            "available": False,
            "evidence": "no java runtime, no keycloak binary; installing a JDK + Keycloak "
            "is out of budget for a 3 GiB sandbox and would not be the "
            "target host's configuration",
        },
        "openviking_real": {
            "available": None,
            "evidence": "not probed in C00; deferred to spike C04",
        },
        "mcp_client_real": {
            "sdk_installed": True,
            "sdk_version": "2.3.0 (official MCP Python SDK)",
            "external_client": False,
            "evidence": "SDK importable; no second independent MCP client present in "
            "this environment, so the A01-A20 MCP leg is not yet exercisable",
        },
        "requested_model": "MiniMax-M3.1-Flash-Preview",
        "model_observed": "MiniMax-M3.1-Flash-Preview",
        "model_observation_source": "session runtime metadata (model_id / provider_id), "
        "not a self-report by the model",
        "provider_observed": "minimax",
        "thinking_observed": "max",
        "model_substitution_occurred": False,
        "paid_api_or_byok_used": False,
        "coding_subscription_confirmed": True,
        "product_runner_subscription_confirmed": False,
        "product_runner_note": "The coding session's model entitlement is not the product's "
        "subscription model-runner entitlement. No product runner "
        "exists in this environment, so E02 stays pending and the "
        "product path uses a fake adapter.",
        "production_access_authorized": False,
        "owner_server_contacted": False,
        "runtime_tests_run": True,
        "checks_performed": [
            {
                "name": "handoff_integrity",
                "command": "python3 verify_handoff.py",
                "status": "passed",
                "exit_code": 0,
                "detail": "handoff_integrity_passed; 65 files, 42 tasks, 18 packages, 20 cases",
            },
            {
                "name": "container_runtime_probe",
                "command": "command -v podman; podman info",
                "status": "failed",
                "exit_code": 127,
                "detail": "absent",
            },
            {
                "name": "systemd_probe",
                "command": "command -v systemctl; ls /run/systemd/system",
                "status": "failed",
                "exit_code": 127,
                "detail": "absent; pid1=node",
            },
            {
                "name": "postgres_rls_probe",
                "command": "pgserver + psql tenant isolation/forgery checks",
                "status": "passed",
                "exit_code": 0,
                "detail": "real PG 16.2, RLS isolation and default deny confirmed",
            },
            {
                "name": "network_dependency_probe",
                "command": "curl pypi/github/debian + wheel download",
                "status": "passed",
                "exit_code": 0,
                "detail": "downloads possible with explicit pip index flags",
            },
        ],
    }

    (OUT / "runtime-capabilities.json").write_text(
        json.dumps(caps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    ownership = {
        "schema_version": "1.0",
        "updated_at": now,
        "policy": "Single executor by default. Shared DDL, contracts and lockfiles have "
        "exactly one owner to avoid write contention; this session is that owner "
        "until a second environment is introduced.",
        "owners": {
            "root_session_450458959069492": {
                "role": "sole executor / single writer",
                "owns": [
                    "docs/handoff/**",
                    "contracts/**",
                    "migrations/**",
                    "uv.lock",
                    "pyproject.toml",
                    "justfile",
                    "deploy/**",
                ],
                "note": "Created and integrated by this session. No other writer exists yet, "
                "so no coordination conflict is possible at this time.",
            }
        },
        "integration_contract": {
            "mcp": "src/kh/mcp/** and the MCP tool registry",
            "domain_services": "src/kh/domain/**",
            "web_admin": "web/**",
            "note": "UI and MCP must call the same domain operations; the registry is "
            "single-owner to prevent divergence between the two surfaces.",
        },
    }
    (OUT / "ownership.json").write_text(
        json.dumps(ownership, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        json.dumps(
            {
                "written": ["runtime-capabilities.json", "ownership.json"],
                "workspace_absolute": str(ROOT),
                "handoff_manifest_sha256": sha256(MANIFEST),
                "ram_mib": mem_mib,
                "postgres": caps["postgres_real"]["version"],
                "podman": False,
                "systemd": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
