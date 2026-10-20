-- migrations/0034_effective_role_runtime.sql
-- Shortcut ONLY: manager is the frozen maximum rank (40), so a direct manager grant
-- can never be beaten by any group rank. Guards and the original ordering are untouched.
CREATE OR REPLACE FUNCTION kb.effective_role(p_principal uuid, p_library uuid)
 RETURNS kb.library_role
 LANGUAGE plpgsql
 STABLE SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'kb'
AS $function$
DECLARE
    v_membership_allowed boolean;
    v_cloud_allowed boolean;
    v_role kb.library_role;
BEGIN
    v_membership_allowed := kb.membership_allows(p_principal);
    IF v_membership_allowed IS NOT TRUE THEN
        RETURN NULL;
    END IF;

    IF COALESCE(current_setting('app.cloud_client', true), '') <> 'true' THEN
        v_cloud_allowed := true;
    ELSE
        v_cloud_allowed := kb.library_policy_allows(p_library, 'cloud');
        IF v_cloud_allowed IS NOT TRUE THEN
            RETURN NULL;
        END IF;
    END IF;

    SELECT g.role
    INTO v_role
    FROM kb.library_grant AS g
    WHERE g.library_id = p_library
      AND g.principal_id = p_principal;

    IF v_role = 'manager' THEN
        RETURN v_role;
    END IF;

    SELECT path.role
    INTO v_role
    FROM (
        SELECT g.role, 0 AS direct
        FROM kb.library_grant AS g
        WHERE g.library_id = p_library
          AND g.principal_id = p_principal

        UNION ALL

        SELECT gg.role, 1 AS direct
        FROM kb.access_group_grant AS gg
        JOIN kb.access_group_member AS m
          ON m.group_id = gg.group_id
        WHERE gg.library_id = p_library
          AND m.principal_id = p_principal
    ) AS path
    ORDER BY kb.role_rank(path.role) DESC, path.direct ASC
    LIMIT 1;

    RETURN v_role;
END;
$function$
