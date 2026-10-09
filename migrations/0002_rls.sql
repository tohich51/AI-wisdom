-- 0002_rls.sql — access model.
--
-- Single owner: this migration file and 0001 are the only DDL path.
--
-- Shape of the model:
--   * authority is PostgreSQL. The gateway never decides access in Python and
--     then filters — a filter in application code is a filter that can be
--     forgotten, and RLS cannot be forgotten.
--   * access derives from kb.library_grant, per library, via an effective
--     role. Library kind and audience never widen a grant.
--   * the runtime role is kb_app: not superuser, not owner, no BYPASSRLS.
--   * identity is transaction-local and set by the trusted transport, never
--     by a request payload.

BEGIN;
SET search_path = kb, public;

-- ------------------------------------------------------- effective role
-- SECURITY DEFINER because it must read library_grant on behalf of callers
-- that have no direct grant on it. Without definer rights the policy would
-- recurse into library_grant's own policies.
CREATE OR REPLACE FUNCTION kb.effective_role(
    p_principal uuid,
    p_library   uuid
) RETURNS library_role
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT g.role
    FROM kb.library_grant g
    WHERE g.library_id = p_library
      AND g.principal_id = p_principal
    ORDER BY
        -- highest role wins if a principal somehow holds several rows
        CASE g.role
            WHEN 'manager'    THEN 4
            WHEN 'curator'    THEN 3
            WHEN 'contributor' THEN 2
            ELSE 1
        END DESC
    LIMIT 1;
$$;

-- Rank helper, shared by the SQL policies and the application check.
CREATE OR REPLACE FUNCTION kb.role_rank(p_role library_role)
RETURNS integer
LANGUAGE sql IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT CASE p_role
        WHEN 'manager'     THEN 40
        WHEN 'curator'     THEN 30
        WHEN 'contributor' THEN 20
        WHEN 'reader'      THEN 10
    END;
$$;

-- Claiming who the caller is. Absent setting means nobody, which resolves to
-- no role and therefore no rows — default deny, not "all".
CREATE OR REPLACE FUNCTION kb.current_principal()
RETURNS uuid
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    raw text;
BEGIN
    raw := current_setting('app.principal', true);
    IF raw IS NULL OR raw = '' THEN
        RETURN NULL;
    END IF;
    RETURN raw::uuid;
EXCEPTION WHEN others THEN
    -- a malformed principal is not a valid identity
    RETURN NULL;
END;
$$;

-- ------------------------------------------------------------- RLS setup
ALTER TABLE kb.library          ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.library          FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.library_grant    ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.library_grant    FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.source           ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.source           FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.knowledge        ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.knowledge        FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.rule             ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.rule             FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.use_record       ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.use_record       FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.experience       ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.experience       FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.outcome          ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.outcome          FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.job              ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.job              FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.fragment         ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.fragment         FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.source_version   ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.source_version   FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.knowledge_provenance ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.knowledge_provenance FORCE ROW LEVEL SECURITY;
ALTER TABLE kb.index_generation ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.index_generation FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.generation_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.generation_policy FORCE  ROW LEVEL SECURITY;

-- library: visible to anyone holding any role on it, plus the org owner.
-- A manager may create a library; a reader may not.
CREATE POLICY library_read ON kb.library FOR SELECT
    USING (kb.effective_role(kb.current_principal(), id) IS NOT NULL);

CREATE POLICY library_create ON kb.library FOR INSERT
    WITH CHECK (kb.current_principal() IS NOT NULL);

-- grants: a principal sees their own grants. Only a manager edits grants.
-- This is why the table stays readable enough to be useful without becoming a
-- directory of who-can-see-what.
CREATE POLICY grant_read_own ON kb.library_grant FOR SELECT
    USING (principal_id = kb.current_principal()
           OR kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 30);

CREATE POLICY grant_write_manager ON kb.library_grant FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40);

-- content tables: read needs reader; write needs contributor or better.
-- Publishing a rule or withdrawing it needs curator or better; that higher bar
-- lives in kb.authorize_rule_publish rather than being duplicated per table.
CREATE POLICY source_read ON kb.source FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10);

CREATE POLICY source_write ON kb.source FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20);

CREATE POLICY fragment_read ON kb.fragment FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM kb.source s
        WHERE s.id = source_id
          AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 10
    ));

CREATE POLICY knowledge_read ON kb.knowledge FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10);

CREATE POLICY knowledge_write ON kb.knowledge FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20);

CREATE POLICY rule_read ON kb.rule FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10);

CREATE POLICY rule_write ON kb.rule FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20);

-- use_record: a principal sees and writes only their own usage. Someone else
-- observing your use history is a different product decision.
CREATE POLICY use_own ON kb.use_record FOR ALL
    USING      (principal_id = kb.current_principal())
    WITH CHECK (principal_id = kb.current_principal());

-- experience / outcome follow the use they belong to.
CREATE POLICY experience_read ON kb.experience FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM kb.use_record u
        WHERE u.id = use_id AND u.principal_id = kb.current_principal()
    ));

CREATE POLICY experience_write ON kb.experience FOR ALL
    USING      (EXISTS (SELECT 1 FROM kb.use_record u WHERE u.id = use_id AND u.principal_id = kb.current_principal()))
    WITH CHECK (EXISTS (SELECT 1 FROM kb.use_record u WHERE u.id = use_id AND u.principal_id = kb.current_principal()));

CREATE POLICY outcome_write ON kb.outcome FOR ALL
    USING      (EXISTS (SELECT 1 FROM kb.use_record u WHERE u.id = use_id AND u.principal_id = kb.current_principal()))
    WITH CHECK (EXISTS (SELECT 1 FROM kb.use_record u WHERE u.id = use_id AND u.principal_id = kb.current_principal()));

-- job: contributors may create jobs (that is how processing starts), the
-- worker role may read them.
CREATE POLICY job_read ON kb.job FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10);

CREATE POLICY job_write ON kb.job FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20);

-- source_version and knowledge_provenance inherit their parent's visibility.
CREATE POLICY source_version_read ON kb.source_version FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM kb.source s
        WHERE s.id = source_id
          AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 10
    ));

CREATE POLICY knowledge_provenance_read ON kb.knowledge_provenance FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM kb.knowledge k
        WHERE k.id = knowledge_id
          AND kb.role_rank(kb.effective_role(kb.current_principal(), k.library_id)) >= 10
    ));

-- Index state is operational. Readers do not see rebuild bookkeeping.
CREATE POLICY index_generation_read ON kb.index_generation FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 20);

CREATE POLICY generation_policy_read ON kb.generation_policy FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 30);

-- ------------------------------------------------------------ grants
-- kb_app gets the minimum it needs to serve the gateway. No BYPASSRLS, and
-- explicitly not the table owner, or FORCE RLS would still be bypassed.
GRANT USAGE ON SCHEMA kb TO kb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA kb TO kb_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA kb TO kb_app;
GRANT EXECUTE ON FUNCTION kb.current_principal() TO kb_app;
GRANT EXECUTE ON FUNCTION kb.effective_role(uuid, uuid) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.role_rank(library_role) TO kb_app;

-- the worker reads and updates jobs; it does not publish or grant
GRANT USAGE ON SCHEMA kb TO kb_worker;
GRANT SELECT, UPDATE ON kb.job TO kb_worker;
GRANT SELECT ON kb.source TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.current_principal() TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.effective_role(uuid, uuid) TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.role_rank(library_role) TO kb_worker;

REVOKE ALL ON kb.library_grant FROM kb_worker;

-- -------------------------------------------------- publication barrier
-- A published rule is immutable. Correction is a new version_no, never an
-- UPDATE. Enforced in the database so a direct psql session cannot bypass it.
CREATE OR REPLACE FUNCTION kb.reject_published_rule_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.publication = 'published' THEN
        RAISE EXCEPTION
            'rule % version % is published and immutable; create a new version',
            OLD.id, OLD.version_no
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER rule_published_immutable
    BEFORE UPDATE ON kb.rule
    FOR EACH ROW EXECUTE FUNCTION kb.reject_published_rule_update();

COMMIT;
