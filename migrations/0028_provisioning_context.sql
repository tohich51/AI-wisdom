BEGIN;

CREATE FUNCTION kb.provisioning_context(
    p_request uuid DEFAULT NULL,
    p_limit integer DEFAULT 1
)
RETURNS TABLE (
    request_id uuid,
    library_id uuid,
    principal_id uuid,
    account_id uuid
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, kb
SET row_security = off
AS $function$
    SELECT
        r.id AS request_id,
        r.library_id AS library_id,
        r.requested_by AS principal_id,
        l.organisation_id AS account_id
    FROM kb.provisioning_request AS r
    JOIN kb.library AS l
      ON l.id = r.library_id
    WHERE (
            (p_request IS NULL
             AND r.state IN ('requested', 'running'))
         OR (p_request IS NOT NULL
             AND r.id = p_request)
          )
      AND EXISTS (
            SELECT 1
            FROM kb.membership AS m
            WHERE m.organisation_id = l.organisation_id
              AND m.principal_id = r.requested_by
              AND m.status = 'active'
          )
      AND kb.role_rank(
              kb.effective_role(r.requested_by, r.library_id)
          ) >= 40
    ORDER BY r.requested_at, r.id
    LIMIT CASE
        WHEN p_limit BETWEEN 1 AND 25 THEN p_limit
        ELSE 0
    END;
$function$;

REVOKE EXECUTE ON FUNCTION kb.provisioning_context(uuid, integer)
    FROM PUBLIC, kb_app, kb_worker;

GRANT EXECUTE ON FUNCTION kb.provisioning_context(uuid, integer)
    TO kb_provisioner;

COMMIT;
