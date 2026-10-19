BEGIN;

CREATE OR REPLACE FUNCTION kb.cancel_semantic_build(p_library uuid,p_generation integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,kb AS $$
DECLARE cancelled boolean;
BEGIN
    IF NOT pg_has_role(session_user,'kb_provisioner','member') THEN
        RAISE EXCEPTION 'projection_writer_required' USING ERRCODE='insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_library::text,91));
    UPDATE kb.index_generation AS g SET state='retired',finished_at=now(),updated_at=now()
    WHERE g.library_id=p_library AND g.generation=p_generation AND g.state='building'
      AND current_setting('app.account', true) = (
          SELECT l.organisation_id::text
          FROM kb.library AS l
          WHERE l.id=g.library_id
      )
      AND (
          g.started_by=kb.current_principal()
          OR EXISTS (
              SELECT 1
              FROM kb.library AS l
              JOIN kb.membership AS m
                ON m.organisation_id=l.organisation_id
               AND m.principal_id=kb.current_principal()
               AND m.status='active'
              WHERE l.id=g.library_id
                AND kb.role_rank(
                    kb.effective_role(m.principal_id,l.id)
                ) >= 40
          )
      );
    cancelled:=FOUND;
    RETURN cancelled;
END;
$$;
REVOKE ALL ON FUNCTION kb.cancel_semantic_build(uuid,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.cancel_semantic_build(uuid,integer) TO kb_provisioner;

CREATE FUNCTION kb.semantic_index_context(
    p_profile_hash text,
    p_library uuid DEFAULT NULL,
    p_limit integer DEFAULT 1000,
    p_rotation bigint DEFAULT 0
)
RETURNS TABLE (
    library_id uuid,
    principal_id uuid,
    account_id uuid
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, kb
SET row_security = off
AS $function$
BEGIN
    IF p_profile_hash IS NULL
       OR char_length(p_profile_hash) <> 64
       OR p_profile_hash COLLATE "C" !~ '^[0-9a-f]{64}$'
       OR p_rotation IS NULL
       OR p_limit IS NULL
       OR p_limit NOT BETWEEN 1 AND 1000
       OR (p_library IS NOT NULL AND p_limit IS DISTINCT FROM 1) THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid semantic_index_context parameters';
    END IF;

    RETURN QUERY
    WITH candidate_libraries AS (
        SELECT DISTINCT
            l.id AS library_id,
            l.organisation_id AS account_id
        FROM kb.library AS l
        LEFT JOIN kb.index_account AS a
          ON a.library_id = l.id
        LEFT JOIN kb.index_generation AS g
          ON g.library_id = l.id
         AND g.state = 'current'
        WHERE kb.library_policy_allows(l.id, 'external') IS TRUE
          AND (
                (p_library IS NOT NULL AND l.id = p_library)
                OR
                (
                    p_library IS NULL
                    AND (
                        a.profile_hash IS NOT NULL
                        OR EXISTS (
                            SELECT 1
                            FROM kb.knowledge AS k
                            WHERE k.library_id = l.id
                              AND k.publication = 'published'
                        )
                    )
                    AND (
                        a.state IS DISTINCT FROM 'ready'
                        OR a.profile_hash IS DISTINCT FROM p_profile_hash
                        OR g.indexed_revision IS DISTINCT FROM l.generation
                        OR g.profile_hash IS DISTINCT FROM p_profile_hash
                    )
                )
          )
    ),
    library_context AS (
        SELECT
            c.library_id,
            c.account_id,
            manager.principal_id
        FROM candidate_libraries AS c
        JOIN LATERAL (
            SELECT m.principal_id
            FROM kb.membership AS m
            WHERE m.organisation_id = c.account_id
              AND m.status = 'active'
              AND kb.role_rank(
                      kb.effective_role(m.principal_id, c.library_id)
                  ) >= 40
            ORDER BY
                kb.role_rank(
                    kb.effective_role(m.principal_id, c.library_id)
                ) DESC,
                m.principal_id ASC
            LIMIT 1
        ) AS manager ON true
    ),
    ranked_contexts AS (
        SELECT
            lc.library_id,
            lc.principal_id,
            lc.account_id,
            count(*) OVER () AS candidate_count,
            row_number() OVER (ORDER BY lc.library_id) - 1 AS candidate_index
        FROM library_context AS lc
    ),
    positioned_contexts AS (
        SELECT
            rc.*,
            CASE
                WHEN rc.candidate_count = 0 THEN 0::bigint
                ELSE mod(
                    mod(
                        mod(
                            p_rotation::numeric,
                            rc.candidate_count::numeric
                        ) + rc.candidate_count::numeric,
                        rc.candidate_count::numeric
                    ) * p_limit::numeric,
                    rc.candidate_count::numeric
                )::bigint
            END AS ring_offset
        FROM ranked_contexts AS rc
    ),
    distanced_contexts AS (
        SELECT
            pc.*,
            CASE
                WHEN pc.candidate_index >= pc.ring_offset
                    THEN pc.candidate_index::numeric - pc.ring_offset::numeric
                ELSE pc.candidate_index::numeric
                     + pc.candidate_count::numeric
                     - pc.ring_offset::numeric
            END AS ring_distance
        FROM positioned_contexts AS pc
        WHERE pc.candidate_count > 0
    )
    SELECT
        dc.library_id,
        dc.principal_id,
        dc.account_id
    FROM distanced_contexts AS dc
    WHERE dc.ring_distance < p_limit
    ORDER BY dc.ring_distance, dc.library_id
    LIMIT p_limit;
END;
$function$;

REVOKE EXECUTE ON FUNCTION kb.semantic_index_context(text, uuid, integer, bigint)
    FROM PUBLIC, kb_app, kb_worker;

GRANT EXECUTE ON FUNCTION kb.semantic_index_context(text, uuid, integer, bigint)
    TO kb_provisioner;

CREATE FUNCTION kb.retire_unavailable_semantic_builds(p_profile_hash text)
RETURNS integer
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, kb
SET row_security = off
AS $function$
DECLARE
    v_library_id uuid;
    v_generation integer;
    v_retired integer;
BEGIN
    IF NOT pg_has_role(session_user,'kb_provisioner','member') THEN
        RAISE EXCEPTION 'projection_writer_required' USING ERRCODE='insufficient_privilege';
    END IF;

    IF p_profile_hash IS NULL
       OR char_length(p_profile_hash) <> 64
       OR p_profile_hash COLLATE "C" !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING
            ERRCODE = '22023',
            MESSAGE = 'invalid semantic_profile_hash';
    END IF;

    IF NOT pg_try_advisory_xact_lock(112187,1) THEN
        RETURN 0;
    END IF;

    SELECT g.library_id,g.generation
    INTO v_library_id,v_generation
    FROM kb.index_generation AS g
    WHERE g.state='building'
      AND g.profile_hash=p_profile_hash
    ORDER BY g.library_id,g.generation DESC
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN 0;
    END IF;

    IF NOT pg_try_advisory_xact_lock(
        hashtextextended(v_library_id::text,91)
    ) THEN
        RETURN 0;
    END IF;

    UPDATE kb.index_generation AS g
    SET state='retired',finished_at=now(),updated_at=now()
    WHERE g.library_id=v_library_id
      AND g.generation=v_generation
      AND g.state='building'
      AND g.profile_hash=p_profile_hash
      AND NOT EXISTS (
          SELECT 1
          FROM kb.semantic_index_context(
              p_profile_hash,g.library_id,1
          ) AS available_context
      );

    GET DIAGNOSTICS v_retired = ROW_COUNT;
    RETURN v_retired;
END;
$function$;

REVOKE ALL ON FUNCTION kb.retire_unavailable_semantic_builds(text)
    FROM PUBLIC, kb_app, kb_worker;

GRANT EXECUTE ON FUNCTION kb.retire_unavailable_semantic_builds(text)
    TO kb_provisioner;

COMMIT;
