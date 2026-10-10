#!/usr/bin/env bash
# Connect to the owner's xorek host for READ-ONLY inspection.
#
# Scope, agreed with the owner on 2026-10-10:
#   * inventory the host for E05 (fresh inventory is a required owner gate)
#   * verify Podman/Quadlet/systemd so E01 can be closed
#   * install nothing, restart nothing, change no configuration
#
# This key has no sudo, no port forwarding, and no write path to anything that
# runs. If you need more than inventory, that is a separate, explicit decision.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="$ROOT/.tools/xorek_key"
KNOWN="$ROOT/.tools/xorek_known_hosts"
CONF="$ROOT/.tools/xorek.env"

[[ -f "$CONF" ]] || { echo "no $CONF — create it with BRIDGE_HOST/BRIDGE_USER"; exit 1; }
set -a; source "$CONF"; set +a
: "${BRIDGE_HOST:?set BRIDGE_HOST in $CONF}"
: "${BRIDGE_USER:?set BRIDGE_USER in $CONF}"
BRIDGE_PORT="${BRIDGE_PORT:-22}"

export GIT_SSH_COMMAND=""  # not a git operation
ssh -i "$KEY" -p "$BRIDGE_PORT" \
    -o IdentitiesOnly=yes \
    -o UserKnownHostsFile="$KNOWN" \
    -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=15 \
    -o ServerAliveInterval=30 \
    "$BRIDGE_USER@$BRIDGE_HOST" "$@"
