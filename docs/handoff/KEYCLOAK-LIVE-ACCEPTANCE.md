# Current-project Keycloak integration

The product uses the existing `knowledge-hub` realm and confidential `kb-gateway`
client. Keycloak runs in its official container, which includes its Java runtime.
No host JVM, separate realm, product database or permanent test deployment is needed.

Browser authentication remains authorization code with PKCE S256 using Authlib.
MFA is handled by Keycloak; PyOTP is a pinned test-only client. Password grant,
implicit grant and the gateway's shared service account remain disabled.

`POST /auth/backchannel-logout` verifies the provider's signed logout token with
the existing PyJWT/JWKS implementation. It requires the exact issuer and browser
client audience, recent iat, exp, jti, sid, and the logout event; nonce is forbidden.
The registered profile requires sid and an empty event object. Body/token sizes
are bounded; tokens, OTP and passwords are never stored or logged.

Migration `0008_provider_logout.sql` adds event deduplication and durable provider
session tombstones. Revocation is scoped by issuer/sid and optional subject.
Shared transactional advisory locks serialize logout against session creation.
A late callback cannot recreate a logged-out session, including across gateways.
The additive migration is compatible with the previous runtime during rollback.

After deploying the receiver, configure only the existing client:

- `frontchannelLogout=false`
- `backchannel.logout.url=<public gateway>/auth/backchannel-logout`
- `backchannel.logout.session.required=true`

Do not reimport the whole realm or change existing owner/colleague credentials.
Preserve the old client representation, image, three Quadlets and gateway env for
rollback; make an encrypted off-host backup before applying the migration.

Live tests are in `tests/integration/identity/test_keycloak_live.py`. They read
server credentials in-process, create `test-e03-*` fixtures and delete exact UUIDs.
The six live cases cover current issuer/client policy, real PKCE login/default
deny, foreign expected issuer, refused password grant, actual OTP enrollment and
challenge, and provider logout of two devices while preserving another user.
Missing capability fails instead of reporting a skipped test as success.

`test_backchannel_logout.py` provides independent real RSA/JWKS/PostgreSQL
regressions, including duplicate delivery, invalid tokens, concurrent logout and
callback, exact session scope and HTTP limits. Its ephemeral native test fixtures
are not a substitute for the separate live Keycloak checks.

Run static/unit/native PostgreSQL gates locally on the exact candidate. Run the
live suite on the current server after receiver registration. A live failure
requires diagnosis or rollback; it does not close other product cards. Record
deployed SHA/image, test output and fixture deletion outside credential files.
The legacy `just acceptance` target still has no acceptance-marked cases; do not
claim that target passed or close unrelated C08-C13 from these six auth checks.

Implementation used MiniMax M3.1 Flash through the existing OAuth subscription.
Codex owns integration, explicit fallbacks for rejected oversized artifacts,
independent checks and rollout. OpenRouter and semantic search are separate work.
