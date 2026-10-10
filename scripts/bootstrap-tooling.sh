#!/usr/bin/env bash
# Reinstall the dev tools that do not survive a sandbox container recreation.
# The workspace and /tmp are NAS-backed and persist; /usr/local/bin is wiped.
# `just` therefore lives in .tools/ and must be reinstallable from a clean
# checkout without touching anything the project depends on.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/.tools"
JUST_VERSION="${JUST_VERSION:-1.36.0}"
mkdir -p "$TOOLS"
if [ -x "$TOOLS/just" ]; then
    echo "just already present: $("$TOOLS/just" --version)"
else
    echo "installing just $JUST_VERSION into .tools/"
    tmp="$(mktemp -d)"
    curl -fsSL "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
        -o "$tmp/just.tgz"
    tar xzf "$tmp/just.tgz" -C "$tmp" just
    install -m755 "$tmp/just" "$TOOLS/just"
    rm -rf "$tmp"
fi
export PATH="$TOOLS:$PATH"
just --version
