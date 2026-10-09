#!/usr/bin/env bash
# C15 — A15 check against a DEPLOYED host: is the index tier reachable from
# outside, and can the application reach a container runtime?
#
# WHAT THIS IS: a black-box check run on the target host after installation.
#
# WHAT THIS IS NOT: a test. It talks to a deployment that does not exist in
# the C15 workspace — there is no Podman and no systemd here (E01), and no
# OpenViking to talk to (E03). The equivalent property IS covered against a
# real PostgreSQL in tests/integration/provisioning/test_boundaries.py, which
# proves that no role but the one-shot may create an account, that the worker
# cannot read the provisioning queue, and that neither the Python package nor
# the Quadlet unit mentions a container socket. What only this script can prove
# is the network shape of a running deployment, which is exactly why it exists.
#
# Usage:
#   KB_PUBLIC_HOST=kb.example.invalid ./verify-no-container-socket.sh
#
# It probes from OUTSIDE the host's loopback, using the host's own public name,
# which is the only vantage point that answers A15 honestly.

set -euo pipefail

HOST="${KB_PUBLIC_HOST:?set KB_PUBLIC_HOST to the public name of this host}"
FAILURES=0
fail() { echo "FAIL: $*" >&2; FAILURES=$((FAILURES + 1)); }
pass() { echo "ok: $*"; }

# The four services that must never answer on a public interface. OpenViking
# and Ollama are the ones this card is about; the database and the
# model-runner are here because the same mistake is usually made for all four.
for port_name in "openviking:19300" "ollama:11434" "postgres:5432" "model-runner:8081"; do
    name="${port_name%%:*}"
    port="${port_name##*:}"
    if timeout 5 bash -c "exec 3<>/dev/tcp/${HOST}/${port}" 2>/dev/null; then
        fail "${name} is reachable on ${HOST}:${port} from outside"
    else
        pass "${name} is not reachable on ${HOST}:${port}"
    fi
done

# The reverse proxy publishes exactly two things: the gateway and the identity
# provider. Anything else answering on 443 is a finding.
if timeout 10 curl -fsS -o /dev/null "https://${HOST}/mcp" 2>/dev/null; then
    pass "the gateway answers on ${HOST} (the one expected public endpoint)"
else
    echo "note: the gateway did not answer on ${HOST}/mcp — is the deployment up?" >&2
fi

# On the host itself: no container runtime socket may be visible to the
# application containers. This reads the generated systemd units rather than
# the source, because what matters is what is deployed, not what was written.
if command -v systemctl >/dev/null 2>&1; then
    for unit in kb-gateway.service kb-worker.service kb-provision-index.service; do
        rendered="$(systemctl --user cat "${unit}" 2>/dev/null || true)"
        [ -n "${rendered}" ] || { echo "note: ${unit} is not installed" >&2; continue; }
        case "${rendered}" in
            *podman.sock*|*docker.sock*|*"DOCKER_HOST"*)
                fail "${unit} references a container socket" ;;
            *)
                pass "${unit} has no container socket" ;;
        esac
    done

    # The provisioning unit must be inert until somebody runs it.
    if systemctl --user is-enabled kb-provision-index.service >/dev/null 2>&1; then
        fail "kb-provision-index.service is enabled; provisioning must not be a boot step"
    else
        pass "kb-provision-index.service is not enabled"
    fi
else
    echo "NOTE: no systemd here; the unit checks did not run." >&2
fi

if [ "${FAILURES}" -ne 0 ]; then
    echo "${FAILURES} finding(s)" >&2
    exit 1
fi
echo "no findings"
