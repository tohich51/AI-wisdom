#!/usr/bin/env bash
# C07 — restart durability check for a deployed gateway.
#
# WHAT THIS IS: a black-box check against a *running* deployment. It signs in
# through the browser flow, keeps only the two cookies, restarts the gateway
# unit, and asks again.
#
# WHAT THIS IS NOT: a test. It talks to a deployment that does not exist in
# this workspace. In the C07 handoff it is recorded as `not_run` with the
# reason "no Podman/systemd here (E01) and no live Keycloak (E03)". The
# equivalent property IS covered by tests/integration/identity/test_browser_flow.py,
# which proves the same thing against a real PostgreSQL with two independent
# gateway instances. What it does not cover is a real process being killed,
# and that is exactly why this script exists.
#
# Usage:
#   KB_BASE_URL=https://kb.example.invalid \
#   KB_USERNAME=... KB_PASSWORD=... \
#   ./verify-session-durability.sh
#
# It drives a real browser login, so it needs an account and a password that
# exists only in the operator's hands. It never reads them from the repository.

set -euo pipefail

BASE_URL="${KB_BASE_URL:?set KB_BASE_URL}"
COOKIE_JAR="$(mktemp -t kb-identity-jar.XXXXXX)"
STATE_FILE="$(mktemp -t kb-identity-state.XXXXXX)"
AUTH_CODE=""
cleanup() { rm -f "$COOKIE_JAR" "$STATE_FILE"; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "1. starting login"
curl -sS -c "$COOKIE_JAR" -b "$COOKIE_JAR" -o /dev/null -D "$STATE_FILE" \
  "${BASE_URL}/auth/login?return_to=/"
AUTH_URL="$(awk 'BEGIN{IGNORECASE=1} /^location:/ {sub(/^[^ ]+ /,""); sub(/\r$/,""); print}' "$STATE_FILE" | tail -1)"
[ -n "$AUTH_URL" ] || fail "no redirect from /auth/login"
echo "   provider redirect captured (${#AUTH_URL} bytes)"

echo "2. completing the browser login at the provider"
# The operator signs in here. The provider redirects back to
# ${BASE_URL}/auth/callback, and we follow it with the login cookie jar.
CALLBACK_URL="$(curl -sS -L -c "$COOKIE_JAR" -b "$COOKIE_JAR" \
  -o /dev/null -w '%{url_effective}' \
  --user "${KB_USERNAME:?}:${KB_PASSWORD:?}" \
  "$AUTH_URL")"
case "$CALLBACK_URL" in
  *"/auth/callback"*) : ;;
  *) fail "provider did not return to the gateway callback (got ${CALLBACK_URL})" ;;
esac

BEFORE="$(curl -sS -b "$COOKIE_JAR" "${BASE_URL}/auth/session")"
echo "   session before restart: ${BEFORE}"
echo "$BEFORE" | grep -q '"authenticated": *true' || fail "not authenticated after login"

echo "3. restarting the gateway"
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user restart kb-gateway.service
  systemctl --user is-active --quiet kb-gateway.service || fail "gateway did not come back"
else
  fail "no systemd on this host; restart the gateway unit by hand and re-run from step 4"
fi

echo "4. asking again after the restart"
AFTER="$(curl -sS -b "$COOKIE_JAR" "${BASE_URL}/auth/session")"
echo "   session after restart: ${AFTER}"
echo "$AFTER" | grep -q '"authenticated": *true' \
  || fail "session did not survive the restart — session state is in gateway RAM"

if [ "$BEFORE" = "$AFTER" ]; then
  echo "OK: the session survived a restart with no change of identity"
else
  echo "OK: the session survived the restart (idle deadline may have moved)"
fi
