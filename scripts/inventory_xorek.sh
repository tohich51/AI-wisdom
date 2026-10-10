#!/usr/bin/env bash
# E05 requires a FRESH server inventory. The snapshot shipped in the handoff is
# from 2026-10-03 and is explicitly marked stale. This collects a current one.
# Read-only: it inspects and prints. It installs nothing and restarts nothing.
set -uo pipefail
run() { "$@" 2>&1 || true; }

echo "=== host ==="
run uname -a
run cat /etc/os-release
echo
echo "=== cpu / memory / disk ==="
run nproc
run free -h
run df -h / /var/lib 2>/dev/null
echo
echo "=== E01: container runtime + init system ==="
run podman --version
run systemctl --version
test -d /run/systemd/system && echo "systemd: RUNNING" || echo "systemd: not the init system"
run systemctl --user is-system-running
echo
echo "=== podman images already present ==="
run podman images
echo
echo "=== E03: keycloak / openviking / ollama / java ==="
run java -version
run podman ps -a --format '{{.Names}} {{.Image}} {{.Status}}'
echo
echo "=== existing workloads (NOT to be touched) ==="
run podman ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
run systemctl list-units --type=service --state=running --no-pager --no-legend
echo
echo "=== network: what is listening ==="
run ss -tlnp
echo
echo "=== disk usage of the places that matter ==="
run du -sh /var/lib/containers 2>/dev/null
run df -h /var/lib/containers 2>/dev/null
