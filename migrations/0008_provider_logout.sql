BEGIN;

CREATE TABLE kb.oidc_logout_event (
    issuer text NOT NULL,
    client_id text NOT NULL,
    jti text NOT NULL,
    provider_session_id text NOT NULL,
    subject text,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, client_id, jti)
);

CREATE TABLE kb.oidc_logged_out_session (
    issuer text NOT NULL,
    provider_session_id text NOT NULL,
    subject text,
    recorded_at timestamptz NOT NULL,
    PRIMARY KEY (issuer, provider_session_id)
);

GRANT SELECT, INSERT, UPDATE, DELETE
    ON kb.oidc_logout_event
    TO kb_app;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON kb.oidc_logged_out_session
    TO kb_app;

REVOKE ALL
    ON kb.oidc_logout_event
    FROM PUBLIC, kb_worker;

REVOKE ALL
    ON kb.oidc_logged_out_session
    FROM PUBLIC, kb_worker;

COMMIT;
