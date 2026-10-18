BEGIN;
SET search_path = kb, public;

ALTER TABLE browser_session
    ADD COLUMN authority_scopes text[],
    ADD COLUMN authority_cloud_client boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT browser_session_authority_scopes_ceiling CHECK (
        authority_scopes IS NULL
        OR (
            cardinality(authority_scopes) > 0
            AND authority_scopes <@ ARRAY[
                'kb:read', 'kb:contribute', 'kb:experience', 'kb:export', 'kb:admin'
            ]::text[]
            AND authority_scopes @> ARRAY['kb:read']::text[]
        )
    );

CREATE TABLE browser_pairing (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    binding_hash            bytea NOT NULL
                            CHECK (octet_length(binding_hash) = 32),
    nonce_hash              bytea NOT NULL UNIQUE
                            CHECK (octet_length(nonce_hash) = 32),
    return_to               text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    expires_at              timestamptz NOT NULL,
    approved_at             timestamptz,
    approved_identity       jsonb,
    authority_scopes        text[],
    authority_cloud_client  boolean,
    credential_expires_at   timestamptz,
    consumed_at             timestamptz,
    cancelled_at            timestamptz,
    CONSTRAINT browser_pairing_lifetime
        CHECK (expires_at > created_at),
    CONSTRAINT browser_pairing_approval_complete CHECK (
        (
            approved_at IS NULL
            AND approved_identity IS NULL
            AND authority_scopes IS NULL
            AND authority_cloud_client IS NULL
            AND credential_expires_at IS NULL
        )
        OR (
            approved_at IS NOT NULL
            AND approved_identity IS NOT NULL
            AND authority_scopes IS NOT NULL
            AND authority_cloud_client IS NOT NULL
            AND credential_expires_at > approved_at
        )
    ),
    CONSTRAINT browser_pairing_authority_scopes CHECK (
        authority_scopes IS NULL
        OR (
            cardinality(authority_scopes) > 0
            AND authority_scopes <@ ARRAY[
                'kb:read', 'kb:contribute', 'kb:experience', 'kb:export', 'kb:admin'
            ]::text[]
            AND authority_scopes @> ARRAY['kb:read']::text[]
        )
    ),
    CONSTRAINT browser_pairing_approved_identity_shape CHECK (
        approved_identity IS NULL
        OR (
            jsonb_typeof(approved_identity) = 'object'
            AND approved_identity ?& ARRAY[
                'issuer', 'subject', 'principal_id', 'account_id', 'scopes',
                'client_id', 'issued_at', 'expires_at', 'provider_session_id'
            ]
            AND approved_identity - ARRAY[
                'issuer', 'subject', 'principal_id', 'account_id', 'scopes',
                'client_id', 'issued_at', 'expires_at', 'provider_session_id'
            ] = '{}'::jsonb
            AND length(approved_identity ->> 'issuer') > 0
            AND length(approved_identity ->> 'subject') > 0
            AND (approved_identity ->> 'principal_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND (approved_identity ->> 'account_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            AND jsonb_typeof(approved_identity -> 'scopes') = 'array'
            AND (
                approved_identity -> 'client_id' = 'null'::jsonb
                OR (
                    jsonb_typeof(approved_identity -> 'client_id') = 'string'
                    AND length(approved_identity ->> 'client_id') > 0
                )
            )
            AND (
                approved_identity -> 'provider_session_id' = 'null'::jsonb
                OR (
                    jsonb_typeof(approved_identity -> 'provider_session_id') = 'string'
                    AND length(approved_identity ->> 'provider_session_id') > 0
                )
            )
            AND (approved_identity ->> 'issued_at')::timestamptz
                < (approved_identity ->> 'expires_at')::timestamptz
            AND credential_expires_at
                <= (approved_identity ->> 'expires_at')::timestamptz
            AND approved_identity -> 'scopes' @> to_jsonb(authority_scopes)
        )
    ),
    CONSTRAINT browser_pairing_terminal_state CHECK (
        (consumed_at IS NULL OR cancelled_at IS NULL)
        AND (consumed_at IS NULL OR approved_at IS NOT NULL)
        AND (consumed_at IS NULL OR consumed_at >= approved_at)
        AND (cancelled_at IS NULL OR cancelled_at >= created_at)
    )
);

CREATE INDEX browser_pairing_open
    ON browser_pairing (expires_at)
    WHERE consumed_at IS NULL AND cancelled_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON kb.browser_pairing TO kb_app;
REVOKE ALL ON kb.browser_pairing FROM PUBLIC, kb_worker;

COMMIT;
