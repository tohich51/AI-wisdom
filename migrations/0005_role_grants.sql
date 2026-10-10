-- 0005_role_grants.sql — close a grant gap left by role enumeration.
--
-- 0002 granted the RLS helper functions by listing roles explicitly. That is
-- correct at the time it was written and quietly wrong afterwards: a later
-- card added kb_provisioner, and because it is not in 0002's list it could
-- not evaluate the RLS policies on the tables it legitimately writes.
--
-- The failure surfaced only when 0004_membership and 0004_provisioning were
-- applied together. Each was green alone, which is the failure mode this
-- project keeps meeting: integration is where per-card gates stop being
-- sufficient.
--
-- Fixed by granting the runtime roles, not by loosening the functions.
-- effective_role is SECURITY DEFINER and only reads the grant table; it
-- decides nothing. role_rank and current_principal are pure helpers.

BEGIN;
SET search_path = kb, public;

GRANT EXECUTE ON FUNCTION kb.effective_role(uuid, uuid)   TO kb_provisioner;
GRANT EXECUTE ON FUNCTION kb.role_rank(library_role)       TO kb_provisioner;
GRANT EXECUTE ON FUNCTION kb.current_principal()           TO kb_provisioner;

-- Any future runtime role must be granted explicitly here too. The test that
-- would have caught this is tests/integration/provisioning/test_boundaries.py;
-- keep a guard so the list cannot silently drift again.
DO $$
DECLARE
    missing text;
BEGIN
    SELECT string_agg(r.rolname, ', ' ORDER BY r.rolname)
      INTO missing
      FROM pg_roles r
     WHERE r.rolname LIKE 'kb\_%'
       AND r.rolname <> 'kb_schema_owner'
       AND NOT has_function_privilege(
               r.rolname, 'kb.effective_role(uuid,uuid)', 'EXECUTE');
    IF missing IS NOT NULL THEN
        RAISE EXCEPTION
            'runtime role(s) % can run under RLS but cannot evaluate its policies; '
            'add a GRANT in this migration', missing;
    END IF;
END $$;

COMMIT;
