BEGIN;

-- CREATE OR REPLACE retains the existing owner and ACL unless explicitly changed.
CREATE OR REPLACE FUNCTION kb.effective_role(p_principal uuid, p_library uuid)
RETURNS kb.library_role
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog,kb
AS $function$
DECLARE
    v_role kb.library_role;
BEGIN
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
    WHERE (
        p_principal IS NULL
        OR NOT EXISTS (
            SELECT 1
            FROM kb.membership AS m
            WHERE m.principal_id = p_principal
              AND m.status <> 'active'
        )
    ) IS TRUE
      AND (
        CASE
            WHEN COALESCE(current_setting('app.cloud_client', true), '') <> 'true' THEN true
            ELSE COALESCE((
                SELECT lp.cloud_mcp_allowed AND lp.external_processing_allowed
                FROM kb.library_policy AS lp
                WHERE lp.library_id = p_library
            ), false)
        END
      ) IS TRUE
    ORDER BY kb.role_rank(path.role) DESC, path.direct ASC
    LIMIT 1;

    RETURN v_role;
END;
$function$;

COMMIT;
