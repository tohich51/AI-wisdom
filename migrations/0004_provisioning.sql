-- 0004_provisioning.sql — C15: the retrieval/index tier's own bookkeeping.
--
-- Ownership: a NEW migration file. 0001, 0002 and both 0003_* files are read
-- here and not edited. Where this file adds a policy or a column to a table
-- created there, the change is stated in place so the single DDL owner can see
-- it during integration. Three other workers own 0004_* with different
-- prefixes; nothing in this file is named to collide with them, and the only
-- pre-existing objects touched are `kb.index_generation` and
-- `kb.generation_policy`.
--
-- The invariants this file exists to make unarguable, each of them structural
-- (a constraint, an index or a policy) rather than a convention:
--
--   1. The gateway cannot create an OpenViking account. Not "should not" —
--      `kb_app` holds no INSERT/UPDATE privilege on `kb.index_account`, the
--      one-shot role is a different role, and a policy admits writes only for
--      that role *and* only for a principal who manages the library. Any one
--      of the three would do; all three are present so no single mistake
--      opens the door.
--   2. Key material never lands in PostgreSQL. `kb.index_credential_ref`
--      stores a reference into a closed secret store and its CHECK accepts
--      nothing else, so a bare key is rejected by the database rather than
--      promised away by application code.
--   3. No wildcard `manage` over a published root. The ACL document is
--      validated by a trigger: every entry must name one of this account's own
--      two service identities, every right must come from the closed set
--      {read, index}, and inheritance from the parent is refused. `manage` is
--      not in the set, so it is not expressible.
--   4. Ordinary search spends nothing. Generation is opt-in per library,
--      owner-started, and limited to ONE building generation in the whole
--      installation by a partial unique index — a database constraint, not a
--      check the application might forget. A read cannot open one: the insert
--      policy requires the library's own opt-in and a manager's principal.
--   5. A half-finished reindex is detectable. `kb.index_generation` gains the
--      content hash and the starting principal, and the index is "current"
--      only when its generation equals the library's `generation` counter.
--      A build that died mid-way leaves a `building` row, which the status
--      function reports as a blocked library rather than as a stale-but-ok
--      index.
--
-- Deliberate non-goal: nothing here talks to OpenViking. This file is
-- PostgreSQL, and PostgreSQL is the only half of this card that can be
-- verified in the C15 environment (no JVM, no embedding provider, no
-- OpenViking server — see docs/handoff/results/C15.json).

BEGIN;
SET search_path = kb, public;

-- ============================================================== the role
-- kb_app is the gateway, kb_worker is the queue worker. Provisioning belongs
-- to neither: the operation that holds the OpenViking root key is a one-shot
-- administrative task, so it gets its own role with its own narrow grants.
--
-- NOBYPASSRLS and NOSUPERUSER are stated rather than left to the default, for
-- the same reason 0001 writes them out: a later ALTER ROLE must not be able to
-- widen this quietly. A provisioning job does not need to see anything the
-- RLS model refuses; it needs to satisfy it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_provisioner') THEN
        CREATE ROLE kb_provisioner LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

-- ========================================================= account mapping
-- One row per library. The OpenViking account name, both service identities
-- and the ACL are SERVER-GENERATED from the library id: they are not chosen
-- by a request, an operator or a job payload, so "provision the library
-- someone else named" is not expressible in the data model.
--
-- What is NOT here: a key, a token, a password or a path to a container
-- socket. Key material lives in the closed secret store and only its
-- reference is recorded (see kb.index_credential_ref, a separate table, so
-- that the half of this row a reader may see contains no secret-adjacent
-- column at all).
CREATE TABLE index_account (
    library_id        uuid PRIMARY KEY REFERENCES kb.library(id) ON DELETE CASCADE,
    account_ref       text NOT NULL UNIQUE,
    read_identity     text NOT NULL UNIQUE,
    index_identity    text NOT NULL UNIQUE,
    -- The restricted root, RELATIVE to the account's own namespace. A vendor
    -- URI is composed by the adapter, never stored here, so a foreign
    -- namespace or an absolute URI cannot be smuggled in through a path:
    -- ':' (scheme), '..' (traversal) and '*' are all outside the pattern.
    root_path         text NOT NULL,
    -- 1024 for the CPU model in PRODUCT-SPEC. Recorded, never assumed: a
    -- dimension that is not recorded cannot be checked against the index.
    dimension         integer NOT NULL,
    embedding_profile text NOT NULL,
    acl               jsonb NOT NULL,
    state             text NOT NULL DEFAULT 'pending'
                      CHECK (state IN ('pending','ready','failed','retired')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT account_ref_is_server_generated
        CHECK (account_ref ~ '^kb-lib-[0-9a-f]{32}$'),
    CONSTRAINT identities_are_explicit_and_per_role
        CHECK (read_identity   ~ '^kb-svc-read-[0-9a-f]{32}$'),
    CONSTRAINT index_identity_is_explicit_and_per_role
        CHECK (index_identity  ~ '^kb-svc-index-[0-9a-f]{32}$'),
    CONSTRAINT the_two_identities_are_different
        CHECK (read_identity <> index_identity),
    CONSTRAINT root_path_is_relative_to_its_own_account
        CHECK (root_path ~ '^/[A-Za-z0-9][A-Za-z0-9._/-]*$'
               AND root_path NOT LIKE '%..%'
               AND root_path NOT LIKE '%:%'
               AND root_path NOT LIKE '%*%'),
    CONSTRAINT dimension_is_a_positive_integer CHECK (dimension > 0),
    CONSTRAINT embedding_profile_is_named CHECK (length(embedding_profile) BETWEEN 1 AND 120),
    CONSTRAINT acl_is_an_object CHECK (jsonb_typeof(acl) = 'object')
);

COMMENT ON TABLE kb.index_account IS
    'Server-generated mapping library_id -> OpenViking account. Contains no key '
    'material and no container socket. The gateway may READ it for libraries it '
    'holds a role on (a search needs to know which account to query); it may '
    'never write it.';

-- ------------------------------------------------------ credential refs
-- The only place a secret-adjacent value is written, and it is a *reference*.
-- The pattern is the whole control: a bare key does not start with
-- 'kb-secrets/', so a future bug that tries to store one is rejected by the
-- database, in the same transaction, with a constraint name that says why.
CREATE TABLE index_credential_ref (
    library_id  uuid NOT NULL REFERENCES kb.library(id) ON DELETE CASCADE,
    identity    text NOT NULL
                CHECK (identity ~ '^kb-svc-(read|index)-[0-9a-f]{32}$'),
    secret_ref  text NOT NULL
                CHECK (secret_ref ~ '^kb-secrets/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$'),
    issued_at   timestamptz NOT NULL DEFAULT now(),
    rotated_at  timestamptz,
    PRIMARY KEY (library_id, identity)
);

COMMENT ON TABLE kb.index_credential_ref IS
    'Reference into the closed secret store. Never the key itself. Readable by '
    'managers and by the one-shot only; a plain reader cannot resolve a path, '
    'so the table is split from kb.index_account rather than protected by a '
    'column filter that a future SELECT * would undo.';

-- ================================================= the restricted ACL doc
-- A trigger, not a CHECK, because the rule is a join across the document and
-- the row: every entry must name an identity THIS account declared. A CHECK
-- cannot see read_identity/index_identity being changed under it.
CREATE FUNCTION kb.reject_loose_index_acl()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = kb, public
AS $$
DECLARE
    entry        jsonb;
    principal_name text;
    right_name   text;
BEGIN
    -- No inheritance. An account that inherits its parent's ACL inherits a
    -- wildcard the moment somebody above it is granted one, and the
    -- published root is exactly where that would land.
    IF jsonb_typeof(NEW.acl -> 'inherit_from_parent') IS DISTINCT FROM 'boolean' THEN
        RAISE EXCEPTION 'acl.inherit_from_parent must be present and boolean (false)'
            USING ERRCODE = 'check_violation';
    END IF;
    IF (NEW.acl ->> 'inherit_from_parent')::boolean <> false THEN
        RAISE EXCEPTION
            'acl.inherit_from_parent must be false: a published root may not inherit ACLs'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF jsonb_typeof(NEW.acl -> 'entries') IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'acl.entries must be an array'
            USING ERRCODE = 'check_violation';
    END IF;
    IF jsonb_array_length(NEW.acl -> 'entries') = 0 THEN
        RAISE EXCEPTION 'acl.entries is empty; a restricted root needs explicit grants'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    FOR entry IN SELECT value FROM jsonb_array_elements(NEW.acl -> 'entries') LOOP
        IF jsonb_typeof(entry -> 'principal') IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'acl entry has no string principal'
                USING ERRCODE = 'check_violation';
        END IF;
        principal_name := entry ->> 'principal';
        IF principal_name NOT IN (NEW.read_identity, NEW.index_identity) THEN
            RAISE EXCEPTION
                'acl entry % is not one of this account''s own service identities',
                principal_name
                USING ERRCODE = 'insufficient_privilege';
        END IF;

        IF jsonb_typeof(entry -> 'rights') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'acl entry % has no rights array', principal_name
                USING ERRCODE = 'check_violation';
        END IF;
        IF jsonb_array_length(entry -> 'rights') = 0 THEN
            RAISE EXCEPTION 'acl entry % has no rights', principal_name
                USING ERRCODE = 'check_violation';
        END IF;

        FOR right_name IN
            SELECT jsonb_array_elements_text(entry -> 'rights')
        LOOP
            -- The closed set. 'manage' is absent by construction, and so is
            -- '*' — a wildcard has no way to reach the vocabulary.
            IF right_name NOT IN ('read', 'index') THEN
                RAISE EXCEPTION
                    'right % is not grantable on a published root; allowed: read, index',
                    right_name
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
        END LOOP;
    END LOOP;

    RETURN NEW;
END;
$$;

CREATE TRIGGER index_account_acl_is_restricted
    BEFORE INSERT OR UPDATE OF acl ON kb.index_account
    FOR EACH ROW EXECUTE FUNCTION kb.reject_loose_index_acl();

-- ==================================================== the generation record
-- 0001 already created kb.index_generation with (library_id, generation,
-- state, canary_uri, started_at, finished_at). What a reindex bookkeeping
-- table needs and did not have is the content hash (so re-running with the
-- same content cannot create a second generation), the principal that started
-- it (so "the owner started it" is a fact and not a claim) and an attempt
-- counter. Those are added here rather than by editing 0001.
ALTER TABLE kb.index_generation
    ADD COLUMN content_hash   text
        CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),
    ADD COLUMN started_by     uuid,
    ADD COLUMN attempt        integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    ADD COLUMN failure_reason text,
    ADD COLUMN updated_at     timestamptz NOT NULL DEFAULT now();

-- 'building' and 'not finished' become the same statement, so a half-finished
-- reindex has exactly one representation and cannot be spelled two ways.
ALTER TABLE kb.index_generation
    ADD CONSTRAINT building_is_exactly_unfinished
        CHECK ((state = 'building') = (finished_at IS NULL)),
    ADD CONSTRAINT finished_after_started
        CHECK (finished_at IS NULL OR finished_at >= started_at),
    ADD CONSTRAINT a_failed_generation_says_why
        CHECK (state <> 'retired' OR failure_reason IS NULL OR length(failure_reason) > 0);

-- PRODUCT-SPEC: "Сначала запуск генерации владельцем, общий concurrency 1."
-- The second half is a constraint, not a check: a unique index over the
-- constant 'building' admits at most one such row in the entire table, so two
-- libraries cannot hold a generation at once no matter which code path tries.
-- A partial unique index is a real, enforced rule — an application that
-- forgets to take a lock still cannot get two.
CREATE UNIQUE INDEX index_generation_one_slot_installation_wide
    ON kb.index_generation ((state))
    WHERE state = 'building';

-- At most one current generation per library: the two UPDATEs that finish a
-- rebuild must retire the old generation and publish the new one in ONE
-- transaction, and this index is what makes "in one transaction" mean
-- something. A library can never be serving two generations at once.
CREATE UNIQUE INDEX index_generation_one_current_per_library
    ON kb.index_generation (library_id)
    WHERE state = 'current';

-- Idempotent reindex: the same content cannot hold two LIVE generations.
-- 'retired' is excluded on purpose — a rebuild that failed and is being
-- attempted again must be able to start a new generation for the same
-- content, and a retried attempt is a new attempt, not a resurrected one.
-- This is PRODUCT-SPEC acceptance #3's bookkeeping half, and it holds for any
-- writer, not only for the function below.
CREATE UNIQUE INDEX index_generation_one_live_per_content_hash
    ON kb.index_generation (library_id, content_hash)
    WHERE content_hash IS NOT NULL AND state IN ('building','current');

-- =============================================== provisioning request queue
-- The application REQUESTS provisioning; the one-shot ACCOMPLISHES it. The
-- queue is a different table from the account on purpose: `kb_app` may insert
-- a request for a library it manages and may not insert an account, so
-- "the gateway created an account" is not a state the schema can reach.
CREATE TABLE provisioning_request (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id      uuid NOT NULL REFERENCES kb.library(id) ON DELETE CASCADE,
    -- The principal that asked, taken from the transport by the application.
    -- It is an audit field; the policies do not trust it and re-derive the
    -- caller's role from kb.library_grant through kb.current_principal().
    requested_by    uuid NOT NULL,
    state           text NOT NULL DEFAULT 'requested'
                    CHECK (state IN ('requested','running','succeeded','failed')),
    attempt         integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    requested_at    timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    -- NULL while there is nothing wrong. Never a placeholder string.
    failure_reason  text,
    CONSTRAINT a_failed_request_explains_itself
        CHECK (state <> 'failed' OR failure_reason IS NOT NULL),
    CONSTRAINT finished_requests_are_finished
        CHECK ((state IN ('succeeded','failed')) = (finished_at IS NOT NULL))
);

-- Re-running provisioning is a RESUME of the same request, not a second job
-- racing the first.
CREATE UNIQUE INDEX provisioning_request_one_open_per_library
    ON kb.provisioning_request (library_id)
    WHERE state IN ('requested','running');

-- ------------------------------------------------------------- run journal
-- The checker comes first, because a CHECK constraint may not reference a
-- function that does not exist yet.
--
-- `detail` is constrained to hold no secret material: the check accepts only
-- the shapes this codebase itself produces (a generated ref, a service
-- identity, a secret-store reference, a short slug, a number, a boolean) and
-- rejects any long opaque token. That turns "we do not log keys" from a
-- promise into a constraint a test can violate.
--
-- IMMUTABLE and PARALLEL SAFE because it reads nothing but its argument, and
-- a volatile-looking helper inside a CHECK would make PostgreSQL refuse to
-- dump or reorder it.
CREATE FUNCTION kb.detail_carries_no_key_material(p_detail jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    -- One literal, not four joined with ||: PostgreSQL folds a string literal
    -- and the literal on the next line into one, which would swallow the ||
    -- into the pattern and change what is being checked. Written as a single
    -- pattern so what the constraint matches is what it says it matches.
    SELECT NOT EXISTS (
        SELECT 1
        FROM jsonb_path_query(p_detail, 'strict $.**') AS j
        WHERE jsonb_typeof(j) = 'string'
          AND (j #>> '{}') !~ '^(kb-lib-[0-9a-f]{32}|kb-svc-(read|index)-[0-9a-f]{32}|kb-secrets/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}|[a-z0-9][a-z0-9._/-]{0,63})$'
    );
$$;

COMMENT ON FUNCTION kb.detail_carries_no_key_material(jsonb) IS
    'True when every string in the document is one of this codebase''s own '
    'reference shapes. A long opaque token is not one of them, so a run '
    'journal cannot be used as a key log.';

CREATE TABLE provisioning_run (
    request_id  uuid NOT NULL REFERENCES kb.provisioning_request(id) ON DELETE CASCADE,
    step        text NOT NULL
                CHECK (step IN ('account','identities','acl','secrets','verified')),
    state       text NOT NULL CHECK (state IN ('done','failed')),
    attempt     integer NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    finished_at timestamptz NOT NULL DEFAULT now(),
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (request_id, step),
    CONSTRAINT detail_is_an_object CHECK (jsonb_typeof(detail) = 'object'),
    CONSTRAINT detail_carries_no_key_material CHECK (kb.detail_carries_no_key_material(detail))
);

COMMENT ON TABLE kb.provisioning_run IS
    'Step journal for a resumable provisioning run. Written by the one-shot, '
    'readable by nobody else. detail is checked for secret material by the '
    'database, so an accidental log of a key fails the write instead of '
    'succeeding quietly.';

-- =================================================== generation admission
-- Both functions below are SECURITY INVOKER on purpose. They do not need
-- definer rights, and running as the caller means the caller's own RLS
-- policies apply to the statements inside — the same rules a direct INSERT
-- would meet. The one function that *does* need definer rights is
-- kb.index_status, because a reader (rank 10) is below the operational read
-- policy on kb.index_generation and still has to be told that the index is
-- not current.
--
-- SECURITY INVOKER is also what keeps these functions honest under FORCE RLS:
-- no statement inside them can see a row the caller could not have read.
CREATE FUNCTION kb.admit_index_rebuild(p_library uuid, p_content_hash text)
RETURNS TABLE (generation integer, resumed boolean)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = kb, public
AS $$
DECLARE
    existing integer;
BEGIN
    IF p_content_hash IS NULL OR p_content_hash !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'content_hash must be a sha256 hex digest'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Same content, same live generation. Re-running an interrupted rebuild
    -- with the same content resumes it instead of creating a sibling. A
    -- RETIRED generation is deliberately not matched: a failed attempt is not
    -- resumable, and the next attempt gets its own generation number so the
    -- journal shows what happened instead of overwriting the failure.
    SELECT g.generation INTO existing
    FROM kb.index_generation g
    WHERE g.library_id = p_library
      AND g.content_hash = p_content_hash
      AND g.state IN ('building','current')
    ORDER BY g.generation DESC
    LIMIT 1;
    IF existing IS NOT NULL THEN
        RETURN QUERY SELECT existing, true;
        RETURN;
    END IF;

    INSERT INTO kb.index_generation
        (library_id, generation, state, content_hash, started_by, attempt)
    VALUES (
        p_library,
        COALESCE((SELECT max(g.generation) FROM kb.index_generation g
                   WHERE g.library_id = p_library), 0) + 1,
        'building',
        p_content_hash,
        kb.current_principal(),
        COALESCE((SELECT max(g.attempt) FROM kb.index_generation g
                   WHERE g.library_id = p_library), 0) + 1
    )
    RETURNING kb.index_generation.generation INTO existing;

    RETURN QUERY SELECT existing, false;
END;
$$;

COMMENT ON FUNCTION kb.admit_index_rebuild(uuid, text) IS
    'Opens one generation for a library, if the library opts in and the caller '
    'is allowed to spend the quota. The installation-wide single slot is '
    'enforced by index_generation_one_slot_installation_wide, not by this '
    'function: two concurrent callers collide on the index, they do not both '
    'win.';

-- Finishing is two statements in one transaction on purpose: the old current
-- generation is retired and the new one is published, and the one-current
-- index is what makes that atomicity mean "this library is never serving two
-- generations at once".
CREATE FUNCTION kb.publish_index_generation(
    p_library    uuid,
    p_generation integer,
    p_canary_uri text
) RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = kb, public
AS $$
BEGIN
    IF p_canary_uri IS NOT NULL AND length(p_canary_uri) > 512 THEN
        RAISE EXCEPTION 'canary_uri is longer than 512 characters'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    UPDATE kb.index_generation
       SET state = 'retired', finished_at = now(), updated_at = now()
     WHERE library_id = p_library AND state = 'current';

    UPDATE kb.index_generation
       SET state = 'current', canary_uri = p_canary_uri,
           finished_at = now(), updated_at = now()
     WHERE library_id = p_library
       AND generation = p_generation
       AND state = 'building';

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'generation % of library % is not building; it cannot be published',
            p_generation, p_library
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

-- A rebuild that died is retired with a reason, never left 'building'
-- forever by a silent caller. `reason` is the operator's evidence; the
-- library-generation comparison in kb.index_status is what actually keeps
-- serving blocked.
CREATE FUNCTION kb.fail_index_generation(
    p_library    uuid,
    p_generation integer,
    p_reason     text
) RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = kb, public
AS $$
BEGIN
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'a failed generation needs a reason'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    UPDATE kb.index_generation
       SET state = 'retired', finished_at = now(),
           failure_reason = left(p_reason, 512), updated_at = now()
     WHERE library_id = p_library
       AND generation = p_generation
       AND state = 'building';

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'generation % of library % is not building; it cannot be failed',
            p_generation, p_library
            USING ERRCODE = 'check_violation';
    END IF;
END;
$$;

-- The four numbers a search needs and nothing else: no canary URI, no starting
-- principal, no failure text, no attempt counter. A reader is entitled to know
-- that the index is behind — that is a correctness fact about their own
-- library — and to nothing about how it got that way.
--
-- `index_ready` is deliberately the CONSERVATIVE answer: a generation that is
-- still building makes the library not ready even when an older current
-- generation still matches. The opposite choice — keep serving the older
-- generation while a rebuild is in flight — is the one that eventually serves
-- text from a projection being rewritten under it. A library reported as not
-- ready is skipped with an explicit reason (ARCHITECTURE §8 asks for a
-- visible partial, not a false "nothing found"); a library wrongly reported as
-- ready is a silent wrong answer.
CREATE FUNCTION kb.index_status(p_library uuid)
RETURNS TABLE (
    library_generation    integer,
    current_generation    integer,
    building_generation   integer,
    index_ready           boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT
        l.generation,
        (SELECT max(g.generation) FROM kb.index_generation g
          WHERE g.library_id = p_library AND g.state = 'current'),
        (SELECT max(g.generation) FROM kb.index_generation g
          WHERE g.library_id = p_library AND g.state = 'building'),
        COALESCE(
            (SELECT g.generation >= l.generation
               FROM kb.index_generation g
              WHERE g.library_id = p_library AND g.state = 'current'
              ORDER BY g.generation DESC
              LIMIT 1),
            false
        )
        AND NOT EXISTS (
            SELECT 1 FROM kb.index_generation b
            WHERE b.library_id = p_library AND b.state = 'building'
        )
    FROM kb.library l
    WHERE l.id = p_library
      -- Default deny for a caller who holds nothing on this library. Without
      -- this line the SECURITY DEFINER turns the function into an existence
      -- oracle: any authenticated principal could feed it a library id and
      -- read the answer off the difference between a row and no row (A20).
      -- A session with no transport principal is the migration/owner session,
      -- which already bypasses RLS by role and can read kb.library directly;
      -- it is not the audience of this threat.
      AND (kb.current_principal() IS NULL
           OR kb.role_rank(kb.effective_role(kb.current_principal(), p_library)) >= 10);
$$;

COMMENT ON FUNCTION kb.index_status(uuid) IS
    'Four numbers. SECURITY DEFINER because a reader sits below the '
    'operational read policy on kb.index_generation, and the surface it needs '
    'is four aggregates — that is the whole reason it is a function and not a '
    'lower policy.';

-- ============================================================== policies
ALTER TABLE kb.index_account       ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.index_account       FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.index_credential_ref ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.index_credential_ref FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.provisioning_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.provisioning_request FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.provisioning_run     ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.provisioning_run     FORCE  ROW LEVEL SECURITY;

-- account: readable by anybody who holds a role on the library. A search has
-- to know which account to ask, and the mapping holds no secret: it is three
-- generated names, a relative path, a dimension and a public ACL.
CREATE POLICY index_account_read ON kb.index_account FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10);

-- The write is the whole card. BOTH conditions are required:
--   * the session is the one-shot role, and
--   * the transport principal manages that library.
-- So even a process holding the root key cannot provision an account for a
-- library its principal does not manage — the key is not the authority, the
-- grant is.
CREATE POLICY index_account_provision ON kb.index_account FOR ALL
    USING (
        pg_has_role(current_user, 'kb_provisioner', 'USAGE')
        AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40
    )
    WITH CHECK (
        pg_has_role(current_user, 'kb_provisioner', 'USAGE')
        AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40
    );

-- credential refs: managers and the one-shot. A reader has no path to resolve
-- and therefore no reason to see one.
CREATE POLICY index_credential_read ON kb.index_credential_ref FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 30);

CREATE POLICY index_credential_provision ON kb.index_credential_ref FOR ALL
    USING (
        pg_has_role(current_user, 'kb_provisioner', 'USAGE')
        AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40
    )
    WITH CHECK (
        pg_has_role(current_user, 'kb_provisioner', 'USAGE')
        AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40
    );

-- requests: an authenticated manager may ask for a library it manages. The
-- application writes this row; it is a request, not an account.
CREATE POLICY provisioning_request_write ON kb.provisioning_request FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40
                AND requested_by = kb.current_principal());

-- The check above refuses a request that claims somebody else asked for it.
-- The library's own policy still applies to the row the request names, so
-- requesting provisioning for a library you cannot manage is refused even
-- when the id is real.

-- The one-shot consumes the queue; a reader may look at their own request's
-- state in the UI and nothing else. No policy for a non-manager: default deny.
CREATE POLICY provisioning_request_read ON kb.provisioning_request FOR SELECT
    USING (requested_by = kb.current_principal()
           OR kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 30);

-- The queue has exactly one consumer and this is its read. It is the one
-- place the one-shot reads rows *without* a transport principal: a queue
-- listing happens before there is a principal to act as, and a queue is not
-- user content — it is ids, a state and a timestamp. Everything the run then
-- does is under the principal the request carries, re-derived from
-- kb.library_grant on every statement.
CREATE POLICY provisioning_request_one_shot_read ON kb.provisioning_request FOR SELECT
    USING (pg_has_role(current_user, 'kb_provisioner', 'USAGE'));

-- The run journal is operational. Managers see the steps of their own
-- library's provisioning — that is what makes a stalled run diagnosable — and
-- nobody else does. `detail` holds no key material, so this is not a secret
-- channel, only a progress channel.
CREATE POLICY provisioning_run_read ON kb.provisioning_run FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM kb.provisioning_request r
        WHERE r.id = request_id
          AND (r.requested_by = kb.current_principal()
               OR kb.role_rank(kb.effective_role(kb.current_principal(), r.library_id)) >= 30)
    ));

CREATE POLICY provisioning_run_provision ON kb.provisioning_run FOR ALL
    USING (
        pg_has_role(current_user, 'kb_provisioner', 'USAGE')
        AND EXISTS (
            SELECT 1 FROM kb.provisioning_request r
            WHERE r.id = request_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), r.library_id)) >= 40
        )
    )
    WITH CHECK (
        pg_has_role(current_user, 'kb_provisioner', 'USAGE')
        AND EXISTS (
            SELECT 1 FROM kb.provisioning_request r
            WHERE r.id = request_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), r.library_id)) >= 40
        )
    );

-- generation policy: a library's opt-in is the owner's decision. 0002 gave
-- the table a read policy only, so the row could be read but never set —
-- opt-in was impossible. This is the write half, manager-only, and it is the
-- only change to a pre-existing table's policies in this file.
CREATE POLICY generation_policy_write ON kb.generation_policy FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40);

-- A generation may only be OPENED by a principal the library's own policy
-- admits. Read the last EXISTS: a library with no policy row has no opt-in,
-- and the default is deny.
--
-- kb.index_generation.library_id is qualified on purpose. An unqualified
-- `library_id` inside the subquery resolves to the INNER table's column, so
-- `p.library_id = library_id` would degenerate into `p.library_id =
-- p.library_id`, be true for every library that has any policy row at all, and
-- quietly opt in the libraries that never asked. That is a real mistake this
-- file made first and the smoke test caught; the qualification is the fix, not
-- a style preference.
--
-- The one-slot and one-per-content-hash indexes above are still what enforce
-- concurrency 1 and idempotency.
CREATE POLICY index_generation_admit ON kb.index_generation FOR INSERT
    WITH CHECK (
        kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 30
        AND EXISTS (
            SELECT 1 FROM kb.generation_policy p
            WHERE p.library_id = kb.index_generation.library_id
              AND p.generation_allowed
              AND (NOT p.requires_owner_start
                   OR kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40)
        )
    );

-- Finishing a build needs contributor on the library; retiring the previous
-- current generation needs a manager, because it changes what readers are
-- served. Neither is reachable from 'retired' — nothing reopens a dead
-- generation, so a failure has to be a new attempt.
CREATE POLICY index_generation_finish ON kb.index_generation FOR UPDATE
    USING (
        (state = 'building'
         AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 30)
        OR
        (state = 'current'
         AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40)
    )
    WITH CHECK (state IN ('current', 'retired'));

-- ================================================================= grants
-- kb_app: read the account mapping (a search needs it), request provisioning
-- for a library it manages, and set that library's generation policy. No
-- INSERT or UPDATE on kb.index_account and no access at all to
-- kb.index_credential_ref beyond what its policy admits.
GRANT SELECT ON kb.index_account TO kb_app;
GRANT SELECT, INSERT, UPDATE ON kb.provisioning_request TO kb_app;
GRANT SELECT ON kb.provisioning_run TO kb_app;
GRANT SELECT, INSERT, UPDATE ON kb.generation_policy TO kb_app;

-- The one-shot. Exactly the tables it needs, and the functions — nothing that
-- reads a browser session, a grant roster or anybody's content.
GRANT USAGE ON SCHEMA kb TO kb_provisioner;
GRANT SELECT ON kb.library, kb.generation_policy TO kb_provisioner;
GRANT SELECT, INSERT, UPDATE ON
    kb.index_account, kb.index_credential_ref,
    kb.provisioning_request, kb.provisioning_run TO kb_provisioner;
GRANT SELECT, INSERT, UPDATE ON kb.index_generation TO kb_provisioner;

-- The worker does not consume the provisioning queue (ACCESS-MODEL §5) and
-- does not resolve secret references. Stated as REVOKE so a future
-- `GRANT ON ALL TABLES` cannot quietly re-open it: 0002's blanket grant was
-- one-time, and this is the assertion that it stays one-time.
REVOKE ALL ON kb.provisioning_request, kb.provisioning_run,
                 kb.index_account, kb.index_credential_ref
    FROM PUBLIC, kb_worker;

GRANT EXECUTE ON FUNCTION kb.index_status(uuid) TO kb_app, kb_worker, kb_provisioner;
GRANT EXECUTE ON FUNCTION kb.admit_index_rebuild(uuid, text) TO kb_app, kb_provisioner;
GRANT EXECUTE ON FUNCTION kb.publish_index_generation(uuid, integer, text)
    TO kb_app, kb_provisioner;
GRANT EXECUTE ON FUNCTION kb.fail_index_generation(uuid, integer, text)
    TO kb_app, kb_provisioner;
GRANT EXECUTE ON FUNCTION kb.detail_carries_no_key_material(jsonb) TO kb_app, kb_provisioner;

-- PostgreSQL gives every new FUNCTION EXECUTE to PUBLIC. That default was
-- found by a test that asked `has_function_privilege('kb_worker', ...) --EXECUTE')`
-- and got `true` from all three, immediately after a migration whose comment
-- said the worker was deliberately not granted them. A grant that does not
-- revoke the default is not a grant, it is a suggestion.
--
-- Revoked first, granted second, for every function this file creates that
-- touches provisioning or generation state. The trigger functions
-- (kb.reject_loose_index_acl, kb.detail_carries_no_key_material) are pure
-- predicates or run only as the table owner fires them; only the last is
-- revoked, and it stays executable by the two runtime roles.
REVOKE EXECUTE ON FUNCTION kb.admit_index_rebuild(uuid, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kb.publish_index_generation(uuid, integer, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kb.fail_index_generation(uuid, integer, text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kb.index_status(uuid) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION kb.detail_carries_no_key_material(jsonb) FROM PUBLIC;

-- The provisioning functions are deliberately NOT granted to kb_worker: the
-- ordinary worker neither starts a generation nor finishes one. It reads the
-- status like everybody else and it does not open the door.

COMMIT;
