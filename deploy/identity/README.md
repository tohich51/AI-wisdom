# C07 — identity deployment templates

Keycloak realm/client configuration for the Knowledge Hub gateway, and the
restart check that proves a browser session is not held in a process.

**No secrets are in this directory.** Every credential is a `__PLACEHOLDER__`
in `oidc.env.example` or a `${VAR}` reference in the realm JSON, which the
Keycloak importer substitutes from the container environment at import time.
The filled environment file is untracked and lives on the target host.

## Files

| File | Purpose |
|---|---|
| `realm-knowledge-hub.json` | Realm + `kb-gateway` confidential client. PKCE `S256` only, no implicit flow, no direct access grants, no service account, no users. |
| `oidc.env.example` | Gateway environment template. Two keys to generate (`KB_OIDC_CLIENT_SECRET`, `KB_IDENTITY_KEY`), the rest to copy from the realm. |
| `verify-session-durability.sh` | Black-box restart check against a running deployment. |

## Why the client is confidential

The gateway keeps the session; the browser never sees a token. That is the
backend-for-frontend shape: an `HttpOnly` cookie is not readable by script,
so an XSS in the UI cannot walk away with a bearer token. A public client
(implicit/PKCE-from-the-browser) would put the token in the page, which is the
opposite of the requirement. The client secret therefore lives in the
gateway's environment file, and the gateway's `AuthorizationCodeFlow` sends it
as `client_secret_post` rather than in an Authorization header, so it does not
travel in a request line.

## Two things a v1 deployment must get right

**The app and the provider are on different hostnames.** A browser cookie is
scoped by registrable domain and ignores the port. If the gateway and Keycloak
are both on `https://127.0.0.1:…`, the `__Host-kb_session` cookie would be
offered to the provider on every request. `kb.example.invalid` and
`idp.example.invalid` is the correct shape, and the reverse proxy (the existing
Caddy, per PRODUCT-SPEC) is the only thing publicly reachable.

**The audience is not the same string for both token kinds.** The browser ID
token is verified against `aud = kb-gateway` (the client id) — that is what
OIDC Core specifies, and it is what `AuthorizationCodeFlow.complete` checks.
The MCP access token is verified against the MCP resource identifier and needs
a fixed audience mapper on a *separate* client; that is C08's card, and no
audience mapper is added here, because an audience mapper that points at an
unset resource would silently widen what the gateway accepts.

## Provisioning outline (owner, on the target host)

1. `just prepare`-equivalent venv, then generate the two keys (see the header
   of `oidc.env.example`).
2. Fill `oidc.env.example` → `deploy/identity/oidc.env` (untracked).
3. Start Keycloak with `--import-realm`, mounting this directory at
   `/opt/keycloak/data/import` and `oidc.env` as its environment file.
4. Create the owner account in the Keycloak admin console. There is no
   self-registration (`registrationAllowed: false`).
5. Grant PostgreSQL `kb.library_grant` rows explicitly. A valid JWT with no
   grant is refused by RLS — see `tests/integration/rls/test_access_model.py`.
6. `deploy/identity/verify-session-durability.sh` against the running gateway.

## Status of the acceptance environment

The realm here is a **template, not an imported realm**. In the C07 workspace
there is no JVM and therefore no Keycloak (`docs/handoff/runtime-capabilities.json`:
`keycloak_real.available = false`). The JWKS, signature, issuer, audience,
header, CSRF, cookie and cross-gateway properties in
`tests/integration/identity/` are verified against a real PostgreSQL 16.2 and
a real local OIDC provider process serving a genuinely RSA-signed JWKS. The
Keycloak leg — realm import, interactive login, MFA, client provisioning, the
audience mapper — is `not_run` and stays behind E03. It is recorded that way in
`docs/handoff/results/C07.json` rather than as a pass.
