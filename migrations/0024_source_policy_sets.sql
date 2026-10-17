BEGIN;

-- These uncorrelated ARRAY subqueries are eligible for execution-local
-- InitPlans. Executor results are not stored in cached plans; every
-- execution reevaluates the arrays with the caller's RLS and active
-- snapshot. This is not a cross-statement or cross-session authority cache.
ALTER POLICY source_read ON kb.source
    USING (
        library_id = ANY (ARRAY(
            SELECT l.id
            FROM kb.library AS l
            WHERE kb.role_rank(
                      kb.effective_role(kb.current_principal(), l.id)
                  ) >= 10
        ))
    );

-- Both USING and WITH CHECK retain the positive rank >= 20 requirement.
ALTER POLICY source_write ON kb.source
    USING (
        library_id = ANY (ARRAY(
            SELECT l.id
            FROM kb.library AS l
            WHERE kb.role_rank(
                      kb.effective_role(kb.current_principal(), l.id)
                  ) >= 20
        ))
    )
    WITH CHECK (
        library_id = ANY (ARRAY(
            SELECT l.id
            FROM kb.library AS l
            WHERE kb.role_rank(
                      kb.effective_role(kb.current_principal(), l.id)
                  ) >= 20
        ))
    );

-- Each child array contains every source visible to the caller, not only
-- manifest-requested IDs. Source SELECT RLS remains authoritative: its read
-- policy admits rank >= 10, while its all-command write policy admits
-- rank >= 20 for SELECT as well.
ALTER POLICY fragment_read ON kb.fragment
    USING (
        source_id = ANY (ARRAY(
            SELECT s.id
            FROM kb.source AS s
        ))
    );

ALTER POLICY source_version_read ON kb.source_version
    USING (
        source_id = ANY (ARRAY(
            SELECT s.id
            FROM kb.source AS s
        ))
    );

-- Split only the fragment writer. Omitting TO retains the default PUBLIC
-- role set; AS PERMISSIVE retains the previous permissive combination.
-- The parent-existence and effective-role expressions below are unchanged.
DROP POLICY fragment_write ON kb.fragment;

CREATE POLICY fragment_write ON kb.fragment
    AS PERMISSIVE
    FOR UPDATE
    USING (
        EXISTS (
            SELECT 1
            FROM kb.source AS s
            WHERE s.id = source_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM kb.source AS s
            WHERE s.id = source_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
        )
    );

CREATE POLICY fragment_insert ON kb.fragment
    AS PERMISSIVE
    FOR INSERT
    WITH CHECK (
        EXISTS (
            SELECT 1
            FROM kb.source AS s
            WHERE s.id = source_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
        )
    );

CREATE POLICY fragment_delete ON kb.fragment
    AS PERMISSIVE
    FOR DELETE
    USING (
        EXISTS (
            SELECT 1
            FROM kb.source AS s
            WHERE s.id = source_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
        )
    );

COMMIT;
