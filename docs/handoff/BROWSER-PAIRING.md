# Browser sign-in through authenticated MCP

The same authenticated human may approve a browser-created, cookie-bound,
one-use 120-second challenge through `approve_browser_login`. The browser page
is `/auth/agent?return_to=/app/maps`. The challenge is public; the HttpOnly
browser binding and OAuth bearer never enter a tool argument or response.

The gateway performs standard Keycloak V2 ID-token exchange and checks exact
issuer, subject, principal, organisation, provider session and authentication
assurance. Product authority is the intersection of the original bearer and AS
exchange scopes. Sessions retain the original client locality, CSRF and current
membership/RLS. Their lifetime is capped by both verified credentials. No token
or password is stored; no owner identity, role or arbitrary session is accepted
from a caller. Interactive sign-in remains available.

Deployment prerequisites, with exact client rollback snapshots:

- `kb-gateway`: standard token exchange enabled, no refresh exchange;
  existing `acr` and `basic` default scopes, existing product scopes optional,
  own-client audience mapper. Password/service-account/implicit grants stay off.
- `kb-mcp`: an audience mapper for `kb-gateway`; signed backchannel logout to
  the existing gateway receiver with session required, preserving unrelated
  client settings. Receiver accepts only the browser and configured local MCP
  client audiences, the same issuer and verified event/signature/SID.
- Do not use the cloud service principal for owner browser login. Do not grant
  it owner library access. Native OAuth refresh picks up audience changes; no
  manual credential extraction is part of the flow.

New 0025 auth metadata is additive and backward compatible. Code rollback can
leave it in place; never drop active session or pairing state to roll back.
The proposed role-helper rewrite was rejected after the isolated-copy benchmark
showed a slowdown; the existing centralized access functions remain unchanged.

Acceptance: real PostgreSQL binding/replay/concurrency/scope/revocation tests,
actual same-realm Keycloak exchange, independent MiniMax and Sol source gates,
fresh encrypted off-host backup plus same-copy restore, exact-image rollout,
then actual owner MCP approval and rendered browser sign-in. TestClient/API
evidence alone does not establish browser acceptance or 2–3 second cold loading.


Admission is bounded under a database advisory lock across gateway replicas:
64 new requests per minute and 128 unexpired rows in total. Completed/cancelled
rows remain only until their original 120-second deadline. The gateway purges
expired pairing state every 60 seconds and on creation. A transient cleanup
failure emits a fixed warning and retries next interval; no credentials are
logged. This bounds the feature's writes/storage and does not claim protection
against every network denial-of-service scenario.

For rollback to code predating scope ceilings, revoke only derived browser
sessions (`authority_scopes IS NOT NULL`) and cancel pending pairings before
starting the older image. Keep ordinary interactive sessions and additive DDL.
Otherwise older code could ignore the new per-session scope restriction.
