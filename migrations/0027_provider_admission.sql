BEGIN;

CREATE TABLE kb.provider_call (
    call_key text PRIMARY KEY,
    token uuid NOT NULL,
    job_kind text NOT NULL,
    job_id uuid NOT NULL,
    job_attempt uuid NOT NULL,
    principal_id uuid NOT NULL,
    account_id uuid NOT NULL,
    state text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT pg_catalog.now(),
    finished_at timestamptz,
    resolution_reason text,
    CONSTRAINT provider_call_call_key_ck
        CHECK (call_key ~ '^[0-9a-f]{64}$'),
    CONSTRAINT provider_call_job_kind_ck
        CHECK (job_kind IN ('ingest', 'research')),
    CONSTRAINT provider_call_state_ck
        CHECK (state IN ('running', 'done', 'unknown', 'resolved')),
    CONSTRAINT provider_call_resolution_reason_ck
        CHECK (
            resolution_reason IS NULL
            OR length(pg_catalog.btrim(resolution_reason)) BETWEEN 1 AND 500
        )
);

CREATE UNIQUE INDEX provider_call_one_active_ck
    ON kb.provider_call ((true))
    WHERE state IN ('running', 'unknown');

ALTER TABLE kb.provider_call ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.provider_call FORCE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE kb.provider_call
    FROM PUBLIC, kb_app, kb_worker, kb_provisioner;

CREATE FUNCTION kb.reserve_provider_call(
    p_key text,
    p_token uuid,
    p_kind text,
    p_job uuid,
    p_job_attempt uuid
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, kb
SET row_security = off
AS $function$
DECLARE
    v_actor uuid;
    v_account uuid;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kb:provider:minimax-owner', 0)
    );

    v_actor := kb.current_principal();
    v_account := NULLIF(
        pg_catalog.current_setting('app.account', true),
        ''
    )::uuid;

    IF v_actor IS NULL OR v_account IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'insufficient_privilege',
            MESSAGE = 'forbidden';
    END IF;

    PERFORM 1
    FROM kb.membership AS m
    WHERE m.principal_id = v_actor
      AND m.organisation_id = v_account
      AND m.status = 'active'
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'insufficient_privilege',
            MESSAGE = 'forbidden';
    END IF;

    IF p_key IS NULL
       OR p_key !~ '^[0-9a-f]{64}$'
       OR p_token IS NULL
       OR p_kind IS NULL
       OR p_kind NOT IN ('ingest', 'research')
       OR p_job IS NULL
       OR p_job_attempt IS NULL
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'invalid_provider_call';
    END IF;

    IF p_kind = 'ingest' THEN
        PERFORM 1
        FROM kb.job AS j
        JOIN kb.library AS l ON l.id = j.library_id
        WHERE j.id = p_job
          AND j.submitted_by = v_actor
          AND l.organisation_id = v_account
          AND j.status = 'running'
          AND j.attempt_token = p_job_attempt
        FOR UPDATE OF j;
    ELSE
        PERFORM 1
        FROM kb.research_run AS r
        WHERE r.id = p_job
          AND r.principal_id = v_actor
          AND r.account_id = v_account
          AND r.status = 'running'
          AND r.attempt_token = p_job_attempt
        FOR UPDATE;
    END IF;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'insufficient_privilege',
            MESSAGE = 'forbidden';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM kb.provider_call AS pc
        WHERE pc.call_key = p_key
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'provider_operation_replay';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM kb.provider_call AS pc
        WHERE pc.state IN ('running', 'unknown')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'provider_busy';
    END IF;

    INSERT INTO kb.provider_call (
        call_key,
        token,
        job_kind,
        job_id,
        job_attempt,
        principal_id,
        account_id,
        state
    )
    VALUES (
        p_key,
        p_token,
        p_kind,
        p_job,
        p_job_attempt,
        v_actor,
        v_account,
        'running'
    );
END;
$function$;

CREATE FUNCTION kb.finish_provider_call(
    p_key text,
    p_token uuid,
    p_state text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, kb
SET row_security = off
AS $function$
DECLARE
    v_actor uuid;
    v_account uuid;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kb:provider:minimax-owner', 0)
    );

    v_actor := kb.current_principal();
    v_account := NULLIF(
        pg_catalog.current_setting('app.account', true),
        ''
    )::uuid;

    IF v_actor IS NULL OR v_account IS NULL THEN
        RAISE EXCEPTION USING
            ERRCODE = 'insufficient_privilege',
            MESSAGE = 'forbidden';
    END IF;

    IF p_state IS NULL
       OR p_state NOT IN ('done', 'unknown', 'not_started')
    THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'invalid_provider_state';
    END IF;

    IF p_state = 'not_started' THEN
        DELETE FROM kb.provider_call AS pc
        WHERE pc.call_key = p_key
          AND pc.token = p_token
          AND pc.principal_id = v_actor
          AND pc.account_id = v_account
          AND pc.state = 'running';
    ELSE
        UPDATE kb.provider_call AS pc
        SET state = p_state,
            finished_at = CASE
                WHEN p_state = 'done' THEN pg_catalog.clock_timestamp()
                ELSE NULL
            END
        WHERE pc.call_key = p_key
          AND pc.token = p_token
          AND pc.principal_id = v_actor
          AND pc.account_id = v_account
          AND pc.state = 'running';
    END IF;

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'insufficient_privilege',
            MESSAGE = 'forbidden';
    END IF;
END;
$function$;

CREATE FUNCTION kb.resolve_provider_call(
    p_key text,
    p_reason text
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, kb
SET row_security = off
AS $function$
DECLARE
    v_reason text;
BEGIN
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kb:provider:minimax-owner', 0)
    );

    v_reason := pg_catalog.btrim(p_reason);

    IF p_key IS NULL OR p_key !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'invalid_provider_call_key';
    END IF;

    IF p_reason IS NULL OR length(v_reason) NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'invalid_resolution_reason';
    END IF;

    UPDATE kb.provider_call AS pc
    SET state = 'resolved',
        resolution_reason = v_reason,
        finished_at = pg_catalog.clock_timestamp()
    WHERE pc.call_key = p_key
      AND pc.state IN ('running', 'unknown');

    IF NOT FOUND THEN
        RAISE EXCEPTION USING
            ERRCODE = 'check_violation',
            MESSAGE = 'provider_call_not_resolvable';
    END IF;
END;
$function$;

REVOKE ALL PRIVILEGES ON FUNCTION kb.reserve_provider_call(text, uuid, text, uuid, uuid)
    FROM PUBLIC, kb_app, kb_worker, kb_provisioner;
REVOKE ALL PRIVILEGES ON FUNCTION kb.finish_provider_call(text, uuid, text)
    FROM PUBLIC, kb_app, kb_worker, kb_provisioner;
REVOKE ALL PRIVILEGES ON FUNCTION kb.resolve_provider_call(text, text)
    FROM PUBLIC, kb_app, kb_worker, kb_provisioner;

GRANT EXECUTE ON FUNCTION kb.reserve_provider_call(text, uuid, text, uuid, uuid)
    TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.finish_provider_call(text, uuid, text)
    TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.resolve_provider_call(text, text)
    TO kb_provisioner;

COMMIT;
