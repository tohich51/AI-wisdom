-- Additive set-based implementation of the existing manifest ACL predicate.
BEGIN;
SET search_path = kb, public;

CREATE OR REPLACE FUNCTION kb.can_read_manifest(p_manifest jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
DECLARE
    item jsonb;
    requested_source_ids uuid[] := '{}'::uuid[];
    requested_version_ids uuid[] := '{}'::uuid[];
    requested_source_hashes text[] := '{}'::text[];
    requested_fragment_ids uuid[] := '{}'::uuid[];
    requested_fragment_source_ids uuid[] := '{}'::uuid[];
    requested_fragment_hashes text[] := '{}'::text[];
    requested_starts integer[] := '{}'::integer[];
    requested_ends integer[] := '{}'::integer[];
    source_id uuid;
    version_id uuid;
    fragment_id uuid;
    fragment_source_id uuid;
    content_hash text;
    text_hash text;
    start_at integer;
    end_at integer;
BEGIN
    IF jsonb_typeof(p_manifest) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_manifest -> 'sources') IS DISTINCT FROM 'array'
       OR jsonb_typeof(p_manifest -> 'fragments') IS DISTINCT FROM 'array' THEN
        RETURN false;
    END IF;

    IF jsonb_array_length(p_manifest -> 'sources') NOT BETWEEN 1 AND 10
       OR jsonb_array_length(p_manifest -> 'fragments') NOT BETWEEN 1 AND 24 THEN
        RETURN false;
    END IF;

    -- Validate and cast sequentially so malformed scalar values cannot be
    -- hoisted into an eager set-based cast. Database visibility is checked once
    -- below through invoker-RLS joins.
    FOR item IN
        SELECT value FROM jsonb_array_elements(p_manifest -> 'sources')
    LOOP
        IF jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR jsonb_typeof(item -> 'source_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'source_version_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'content_hash') IS DISTINCT FROM 'string' THEN
            RETURN false;
        END IF;

        source_id := (item ->> 'source_id')::uuid;
        version_id := (item ->> 'source_version_id')::uuid;
        content_hash := item ->> 'content_hash';
        IF source_id IS NULL OR version_id IS NULL
           OR source_id = ANY(requested_source_ids) THEN
            RETURN false;
        END IF;

        requested_source_ids := array_append(requested_source_ids, source_id);
        requested_version_ids := array_append(requested_version_ids, version_id);
        requested_source_hashes := array_append(requested_source_hashes, content_hash);
    END LOOP;

    FOR item IN
        SELECT value FROM jsonb_array_elements(p_manifest -> 'fragments')
    LOOP
        IF jsonb_typeof(item) IS DISTINCT FROM 'object'
           OR jsonb_typeof(item -> 'fragment_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'source_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'text_hash') IS DISTINCT FROM 'string'
           OR jsonb_typeof(item -> 'excerpt_start') IS DISTINCT FROM 'number'
           OR jsonb_typeof(item -> 'excerpt_end') IS DISTINCT FROM 'number' THEN
            RETURN false;
        END IF;

        fragment_id := (item ->> 'fragment_id')::uuid;
        fragment_source_id := (item ->> 'source_id')::uuid;
        text_hash := item ->> 'text_hash';
        start_at := (item ->> 'excerpt_start')::integer;
        end_at := (item ->> 'excerpt_end')::integer;
        IF fragment_id IS NULL OR fragment_source_id IS NULL
           OR fragment_id = ANY(requested_fragment_ids)
           OR NOT fragment_source_id = ANY(requested_source_ids)
           OR start_at IS NULL OR end_at IS NULL THEN
            RETURN false;
        END IF;

        requested_fragment_ids := array_append(requested_fragment_ids, fragment_id);
        requested_fragment_source_ids := array_append(
            requested_fragment_source_ids, fragment_source_id
        );
        requested_fragment_hashes := array_append(requested_fragment_hashes, text_hash);
        requested_starts := array_append(requested_starts, start_at);
        requested_ends := array_append(requested_ends, end_at);
    END LOOP;

    RETURN (
        WITH requested_sources AS MATERIALIZED (
            SELECT u.source_id, u.source_version_id, u.content_hash
            FROM unnest(
                requested_source_ids,
                requested_version_ids,
                requested_source_hashes
            ) AS u(source_id, source_version_id, content_hash)
        ),
        requested_fragments AS MATERIALIZED (
            SELECT
                u.fragment_id,
                u.source_id,
                u.text_hash,
                u.excerpt_start,
                u.excerpt_end
            FROM unnest(
                requested_fragment_ids,
                requested_fragment_source_ids,
                requested_fragment_hashes,
                requested_starts,
                requested_ends
            ) AS u(
                fragment_id,
                source_id,
                text_hash,
                excerpt_start,
                excerpt_end
            )
        )
        SELECT
            NOT EXISTS (
                SELECT 1
                FROM requested_sources AS requested
                LEFT JOIN kb.source AS s ON s.id = requested.source_id
                LEFT JOIN kb.source_version AS v
                  ON v.id = requested.source_version_id
                 AND v.source_id = s.id
                WHERE s.id IS NULL
                   OR v.id IS NULL
                   OR s.publication = 'withdrawn'
                   OR s.content_hash IS DISTINCT FROM requested.content_hash
                   OR v.content_hash IS DISTINCT FROM requested.content_hash
            )
            AND NOT EXISTS (
                SELECT 1
                FROM requested_fragments AS requested
                LEFT JOIN kb.fragment AS f ON f.id = requested.fragment_id
                WHERE f.text IS NULL
                   OR f.source_id IS DISTINCT FROM requested.source_id
                   OR requested.excerpt_start < 0
                   OR requested.excerpt_end <= requested.excerpt_start
                   OR requested.excerpt_end > char_length(f.text)
                   OR encode(
                       sha256(convert_to(f.text, 'UTF8')), 'hex'
                   ) IS DISTINCT FROM requested.text_hash
            )
            AND NOT EXISTS (
                SELECT 1
                FROM requested_sources AS selected
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM requested_fragments AS requested
                    WHERE requested.source_id = selected.source_id
                )
            )
    );
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN false;
END;
$$;

REVOKE ALL ON FUNCTION kb.can_read_manifest(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.can_read_manifest(jsonb) TO kb_app, kb_worker;
COMMIT;
