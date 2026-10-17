BEGIN;

-- CREATE OR REPLACE retains the existing owner and ACL unless explicitly changed.
CREATE OR REPLACE FUNCTION kb.membership_allows(p_principal uuid)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $function$
DECLARE
    v_allowed boolean;
BEGIN
    SELECT
        p_principal IS NULL
        OR NOT EXISTS (
            SELECT 1
            FROM kb.membership AS m
            WHERE m.principal_id = p_principal
              AND m.status <> 'active'
        )
    INTO v_allowed;

    RETURN v_allowed;
END;
$function$;

CREATE OR REPLACE FUNCTION kb.library_policy_allows(p_library uuid, p_purpose text)
RETURNS boolean
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog,kb
AS $function$
DECLARE
    v_allowed boolean;
BEGIN
    SELECT CASE p_purpose
        WHEN 'external' THEN COALESCE((
            SELECT lp.external_processing_allowed
            FROM kb.library_policy AS lp
            WHERE lp.library_id = p_library
        ), true)
        WHEN 'cloud' THEN COALESCE((
            SELECT lp.cloud_mcp_allowed AND lp.external_processing_allowed
            FROM kb.library_policy AS lp
            WHERE lp.library_id = p_library
        ), false)
        WHEN 'export' THEN COALESCE((
            SELECT lp.export_allowed
            FROM kb.library_policy AS lp
            WHERE lp.library_id = p_library
        ), true)
        WHEN 'generation' THEN COALESCE((
            SELECT gp.generation_allowed
            FROM kb.generation_policy AS gp
            WHERE gp.library_id = p_library
        ), false) AND kb.library_policy_allows(p_library, 'external')
        ELSE false
    END
    INTO v_allowed;

    RETURN v_allowed;
END;
$function$;

REVOKE ALL ON FUNCTION kb.library_policy_allows(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.library_policy_allows(uuid, text)
    TO kb_app, kb_worker, kb_provisioner;

CREATE OR REPLACE FUNCTION kb.effective_role(p_principal uuid, p_library uuid)
RETURNS kb.library_role
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog,kb
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
$function$;

COMMIT;
