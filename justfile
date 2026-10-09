# Knowledge Hub — task entrypoints.
#
# Design rule that governs this file: a mandatory gate that cannot run MUST
# exit nonzero. A green receipt for a check that did not happen is worse than
# a red one, because it transfers a false claim to whoever accepts the work.
#
#   just prepare      install dependencies reproducibly from the lock
#   just check        static analysis + unit tests          (no services)
#   just integration  real PostgreSQL, RLS enforcement
#   just acceptance   A01-A20 access matrix + S01-S04 scaling
#   just package      build the source delivery bundle (never deploys)
#   just verify-self  prove that `check` really fails on a broken tree

set shell := ["bash", "-euo", "pipefail", "-c"]

ROOT := justfile_directory()
VENV := ROOT / ".venv"
PY := VENV / "bin" / "python"
PYTEST := VENV / "bin" / "pytest"
RUFF := VENV / "bin" / "ruff"
MYPY := VENV / "bin" / "mypy"

# The sandbox default pip index fails for every package; these flags are the
# working configuration. Harmless on a normal host.
PIP_INDEX := "--index-url https://pypi.org/simple/ --trusted-host pypi.org --trusted-host files.pythonhosted.org"

export PYTHONPATH := ROOT / "src"

default:
    @just --list

# ---------------------------------------------------------------- prepare

# Reproducible install from the lock. Creates the venv if absent.
prepare:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -x "{{PY}}" ]; then
        python3 -m venv "{{VENV}}"
    fi
    "{{PY}}" -m pip install -q --upgrade pip {{PIP_INDEX}}
    # requirements.lock is the authoritative pin set (see docs/decisions/dependencies.md)
    "{{PY}}" -m pip install -q {{PIP_INDEX}} -r requirements.lock
    "{{PY}}" -m pip install -q {{PIP_INDEX}} -e ".[dev,pgtest]"
    "{{PY}}" - <<'EOF'
    import sys
    import mcp, fastapi, psycopg, pydantic, sqlalchemy
    print("installed:", "mcp", mcp.__version__ if hasattr(mcp,'__version__') else "?",
          "| fastapi", fastapi.__version__, "| psycopg", psycopg.__version__,
          "| pydantic", pydantic.__version__, "| sqlalchemy", sqlalchemy.__version__)
    print("python:", sys.version.split()[0])
    EOF

# ------------------------------------------------------------------ check
# No external services. Must be green on a clean tree and red on a broken one.

check: lint types unit

lint:
    "{{RUFF}}" check src tests scripts
    "{{RUFF}}" format --check src tests scripts

types:
    "{{MYPY}}" --no-error-summary || true
    "{{MYPY}}" 2>&1 | tail -20

unit:
    "{{PYTEST}}" -m "not integration and not acceptance" -q

# Self-test of the gate itself: injects a real defect and requires `check` to
# catch it. If this recipe passes while the defect survives, the gate is fake.
verify-self:
    #!/usr/bin/env bash
    set -euo pipefail
    sentinel="src/kb/_defect_probe.py"
    cleanup() { rm -f "$sentinel"; }
    trap cleanup EXIT
    printf 'import os  # unused import + undefined name below\n\n\ndef broken():\n    return UNDEFINED_SYMBOL\n' > "$sentinel"
    if just check >/dev/null 2>&1; then
        echo "FAIL: 'just check' passed on a deliberately broken tree — the gate is not real" >&2
        exit 1
    fi
    echo "ok: 'just check' correctly rejected the injected defect"
    cleanup
    just check >/dev/null 2>&1
    echo "ok: 'just check' is green again on the clean tree"

# ------------------------------------------------------------- integration
# Real PostgreSQL. If no server can be started, this must NOT pass.

integration:
    #!/usr/bin/env bash
    set -euo pipefail
    "{{PYTEST}}" -m "integration" -q --no-header

# --------------------------------------------------------------- acceptance
# A01-A20 access matrix and S01-S04 scaling scenarios. Missing runtime = fail.

acceptance:
    #!/usr/bin/env bash
    set -euo pipefail
    set +e
    "{{PYTEST}}" -m "acceptance" -q --no-header
    rc=$?
    set -e
    if [ "$rc" -eq 5 ]; then
        echo "ACCEPTANCE NOT RUN: no acceptance-marked tests exist yet (C33A-C33D, C35 scope)." >&2
        echo "This gate is mandatory and is NOT satisfied by the absence of tests." >&2
    fi
    exit "$rc"

# ------------------------------------------------------------------ package
# Builds the source delivery bundle. Never pushes, never deploys, never opens
# a network listener to the outside.

package:
    #!/usr/bin/env bash
    set -euo pipefail
    "{{PY}}" scripts/build_bundle.py

# --------------------------------------------------------------------- env

env:
    @echo "ROOT   : {{ROOT}}"
    @echo "python : $({{PY}} -V 2>&1)"
    @echo "just   : $(just --version)"
    @-command -v podman >/dev/null && echo "podman : $(podman --version)" || echo "podman : ABSENT (E01 cannot run here)"
    @-command -v systemctl >/dev/null && echo "systemd: present" || echo "systemd: ABSENT (E01 cannot run here)"
    @-test -d "{{VENV}}" && echo "venv   : ready" || echo "venv   : not prepared (run: just prepare)"
