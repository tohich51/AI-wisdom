-- 0033 — resolve one grant recipient by exact email, for grant setup. A manager
-- can grant by principal id but cannot *name* a colleague (a non-admin reads only
-- its own membership row), so for a caller who already manages this library this
-- answers one narrow question: which principal holds *this* exact address in this
-- organisation. It is deliberately a targeted address-existence oracle — that is
-- the grant-setup affordance — and exposes no roster, prefix, count or paging.

CREATE FUNCTION kb.resolve_grant_recipient(p_library uuid, p_email text)
RETURNS TABLE(principal_id uuid, display_name text, email text)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, kb
AS $fn$
DECLARE
    v_actor   uuid := kb.current_principal();
    v_account uuid := NULLIF(current_setting('app.account', true), '')::uuid;
    v_org     uuid;
BEGIN
    SELECT l.organisation_id INTO v_org FROM kb.library AS l WHERE l.id = p_library;

    -- One guard, one 42501: identity, library, verified account, active membership, role.
    IF v_actor IS NULL OR v_account IS NULL OR v_org IS NULL
       OR v_account <> v_org
       OR NOT EXISTS (SELECT 1 FROM kb.membership AS m
                       WHERE m.organisation_id = v_org
                         AND m.principal_id = v_actor
                         AND m.status = 'active')
       OR COALESCE(kb.role_rank(kb.effective_role(v_actor, p_library)), 0) < 40
    THEN
        RAISE EXCEPTION 'forbidden' USING ERRCODE = '42501';
    END IF;

    -- Whole-address, case-folded, trimmed, this organisation's active members; at
    -- most two rows so the caller can tell ambiguous from unique without a count.
    RETURN QUERY
    SELECT m.principal_id, m.display_name, m.email
      FROM kb.membership AS m
     WHERE m.organisation_id = v_org
       AND m.status = 'active'
       AND lower(m.email) = lower(btrim(p_email))
     ORDER BY m.principal_id
     LIMIT 2;
END;
$fn$;

COMMENT ON FUNCTION kb.resolve_grant_recipient(uuid, text) IS
    'Exact-email lookup of grant recipients in one library''s own organisation. Writes nothing. Refuses with 42501 forbidden unless the caller''s verified account is that organisation, the caller is an active member of it, and the caller manages the library. Zero rows and two rows both mean "not one recipient".';

REVOKE ALL ON FUNCTION kb.resolve_grant_recipient(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.resolve_grant_recipient(uuid, text) TO kb_app;
