-- 0003_identity_sessions.sql — C07: durable OIDC login transactions and
-- durable browser sessions.
--
-- Ownership: 0001/0002 are the only DDL path for existing objects and are not
-- touched here. This file only ADDS tables. It is applied by the same single
-- DDL owner as 0001/0002; it is named 0003 on purpose so it cannot collide
-- with the parallel catalog migration series.
--
-- The invariant this file exists to enforce: gateway session state and the
-- pending OAuth transaction (state + PKCE verifier) live in PostgreSQL, not
-- in the memory of one gateway process. S01 in SCALING.md requires that a
-- browser request alternates between two gateways and a restart of one
-- changes nothing. That is only true if the bytes are here.
--
-- Deliberately absent: access tokens, refresh tokens and ID tokens. A session
-- row is a credential, not a token cache. A database dump therefore contains
-- no replayable bearer token, and nothing in this schema can be pasted into a
-- curl command. The token exists only in the memory of the request that
-- exchanged the authorization code, and is discarded there.
--
-- Row level security: NOT enabled on these two tables, on purpose. RLS here
-- would key on app.principal, but reading a session is precisely the step
-- that *establishes* the principal — the check would be circular, and a
-- policy that is always false is a policy that always denies. The credential
-- material is protected instead by:
--   * reachability — only kb_app (the gateway) has any privilege on these
--     tables; kb_worker and PUBLIC are explicitly revoked, and the
--     integration test asserts the worker really is refused;
--   * addressability — a row is reachable only through a 256-bit random
--     secret that is stored as a SHA-256 digest, so a guess costs 2^256 and
--     a dump reveals no usable cookie value;
--   * liveness — revoked, idle-expired and absolutely-expired rows are not
--     selectable, so a leaked-but-old cookie is inert.
-- The subject tables in 0002 remain fully RLS-protected; nothing here widens
-- access to them.

BEGIN;
SET search_path = kb, public;

-- ------------------------------------------------- pending OAuth transaction
-- One row per GET /auth/login. The browser holds `state`; the database holds
-- only its digest, so a read of this table (or a backup) does not hand an
-- attacker a value that is currently in flight to a real user agent.
--
-- The PKCE code_verifier is sealed with Fernet (KB_IDENTITY_KEY) rather than
-- stored in the clear: between /auth/login and /auth/callback it is a live
-- credential for the pending authorization code.
CREATE TABLE auth_login_transaction (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    state_hash          bytea NOT NULL UNIQUE
                        CHECK (octet_length(state_hash) = 32),
    -- digest of a short-lived cookie the browser was given at /auth/login.
    -- This is the login-CSRF binding: `state` alone stops an attacker from
    -- forging a callback, but an attacker who *starts* a login knows their
    -- own state, so the callback must also arrive in a browser that was
    -- handed this value. Digest only, for the same reason as state_hash.
    binding_hash        bytea NOT NULL
                        CHECK (octet_length(binding_hash) = 32),
    code_verifier_sealed text NOT NULL,
    code_challenge      text NOT NULL,
    redirect_uri        text NOT NULL,
    -- where to send the browser afterwards. Nullable and app-validated to be
    -- a same-origin relative path; an absent return target is genuinely
    -- unknown, not the string "home".
    return_to           text,
    nonce               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    -- login is a browser round trip, not a background job. A transaction
    -- that outlives it is a transaction nobody will complete.
    expires_at          timestamptz NOT NULL,
    consumed_at         timestamptz,
    -- the gateway instance that opened the login. Operational only.
    created_by_instance text NOT NULL
);

CREATE INDEX auth_login_transaction_open
    ON auth_login_transaction (expires_at)
    WHERE consumed_at IS NULL;

-- --------------------------------------------------------- browser session
-- The durable half of "log in to the browser UI". A cookie carries
-- "<session uuid>.<256-bit secret>"; only the digest of the secret is stored,
-- so the row cannot be turned back into a cookie.
CREATE TABLE browser_session (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    secret_hash         bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    -- double-submit CSRF. The browser must read the CSRF token from a
    -- non-HttpOnly cookie and echo it in a header, so the expected value is
    -- kept server-side as a digest too: a forged cookie+header pair that the
    -- attacker chose both halves of cannot match.
    csrf_token_hash     bytea NOT NULL CHECK (octet_length(csrf_token_hash) = 32),
    principal_id        uuid NOT NULL,
    account_id          uuid,
    issuer              text NOT NULL,
    subject             text NOT NULL,
    -- the provider's own session id (Keycloak `session_state`), when the
    -- provider sends one. NULL means the provider did not send one.
    provider_session_id text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    last_served_by_instance text,
    idle_expires_at     timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at          timestamptz,
    revoked_reason      text,
    created_by_instance text NOT NULL,
    CONSTRAINT idle_window_within_absolute
        CHECK (idle_expires_at <= absolute_expires_at),
    CONSTRAINT absolute_expiry_is_a_deadline
        CHECK (absolute_expires_at > created_at)
);

-- A provider session is not a browser session. Keycloak's `session_state`
-- identifies one SSO session, and a person signed in on a laptop and a phone
-- has two browser sessions sharing it — so this is a plain index, never a
-- unique one. It is what a future backchannel logout would use to revoke
-- every device at once.
CREATE INDEX browser_session_provider_session
    ON browser_session (issuer, provider_session_id)
    WHERE provider_session_id IS NOT NULL;

CREATE INDEX browser_session_principal
    ON browser_session (principal_id);

CREATE INDEX browser_session_expiry
    ON browser_session (absolute_expires_at)
    WHERE revoked_at IS NULL;

-- ------------------------------------------------------------- privileges
-- Only the gateway reaches credential material. The worker processes jobs
-- and has no business knowing that a browser session exists.
GRANT SELECT, INSERT, UPDATE, DELETE ON kb.auth_login_transaction TO kb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb.browser_session TO kb_app;

REVOKE ALL ON kb.auth_login_transaction FROM PUBLIC, kb_worker;
REVOKE ALL ON kb.browser_session FROM PUBLIC, kb_worker;

COMMIT;
