-- Derived, library-scoped concept embeddings and undirected similarity edges.
BEGIN;

CREATE FUNCTION kb.valid_concept_embedding(
    p_values double precision[], p_dimension integer
) RETURNS boolean LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $$
DECLARE
    item double precision;
    square_sum numeric := 0;
BEGIN
    IF p_values IS NULL OR p_dimension IS NULL THEN
        RETURN false;
    END IF;
    IF p_dimension < 1
       OR array_ndims(p_values) <> 1
       OR cardinality(p_values) <> p_dimension THEN
        RETURN false;
    END IF;
    FOR item IN SELECT unnest(p_values) LOOP
        IF item IS NULL
           OR item::text IN ('NaN', 'Infinity', '-Infinity')
           OR item < -1.0000001 OR item > 1.0000001 THEN
            RETURN false;
        END IF;
        square_sum := square_sum + item::numeric * item::numeric;
    END LOOP;
    RETURN square_sum > 0 AND square_sum <= 1.0001;
END;
$$;
REVOKE ALL ON FUNCTION kb.valid_concept_embedding(double precision[],integer) FROM PUBLIC;

CREATE TABLE kb.concept_embedding (
    concept_id uuid NOT NULL REFERENCES kb.semantic_concept(id) ON DELETE RESTRICT,
    library_id uuid NOT NULL REFERENCES kb.library(id) ON DELETE RESTRICT,
    profile_hash text NOT NULL CHECK(profile_hash ~ '^[0-9a-f]{64}$'),
    text_hash text NOT NULL CHECK(text_hash ~ '^[0-9a-f]{64}$'),
    embedding double precision[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(concept_id,profile_hash,text_hash),
    CONSTRAINT concept_embedding_vector_valid CHECK(
        kb.valid_concept_embedding(embedding,4096) IS TRUE
    )
);

CREATE TABLE kb.concept_similarity (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id uuid NOT NULL REFERENCES kb.library(id) ON DELETE RESTRICT,
    profile_hash text NOT NULL CHECK(profile_hash ~ '^[0-9a-f]{64}$'),
    source_concept uuid NOT NULL,
    target_concept uuid NOT NULL,
    source_text_hash text NOT NULL CHECK(source_text_hash ~ '^[0-9a-f]{64}$'),
    target_text_hash text NOT NULL CHECK(target_text_hash ~ '^[0-9a-f]{64}$'),
    score double precision NOT NULL CHECK(score >= 0.65 AND score <= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(source_concept < target_concept),
    UNIQUE(
        library_id,profile_hash,source_concept,target_concept,
        source_text_hash,target_text_hash
    ),
    FOREIGN KEY(source_concept,profile_hash,source_text_hash)
        REFERENCES kb.concept_embedding(concept_id,profile_hash,text_hash)
        ON DELETE RESTRICT,
    FOREIGN KEY(target_concept,profile_hash,target_text_hash)
        REFERENCES kb.concept_embedding(concept_id,profile_hash,text_hash)
        ON DELETE RESTRICT
);

CREATE INDEX concept_embedding_lookup
    ON kb.concept_embedding(library_id,profile_hash,text_hash,created_at,concept_id);
CREATE INDEX concept_similarity_source
    ON kb.concept_similarity(library_id,profile_hash,source_concept,target_concept);
CREATE INDEX concept_similarity_target
    ON kb.concept_similarity(library_id,profile_hash,target_concept,source_concept);

CREATE TABLE kb.concept_similarity_build (
    library_id uuid NOT NULL REFERENCES kb.library(id) ON DELETE RESTRICT,
    profile_hash text NOT NULL CHECK(profile_hash ~ '^[0-9a-f]{64}$'),
    selection_hash text NOT NULL CHECK(selection_hash ~ '^[0-9a-f]{64}$'),
    concept_ids uuid[] NOT NULL,
    edge_ids uuid[] NOT NULL DEFAULT ARRAY[]::uuid[],
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT concept_similarity_build_concepts_valid CHECK(
        cardinality(concept_ids) BETWEEN 1 AND 200
    ),
    UNIQUE(library_id,profile_hash,selection_hash)
);

ALTER TABLE kb.concept_embedding ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.concept_embedding FORCE ROW LEVEL SECURITY;
ALTER TABLE kb.concept_similarity ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.concept_similarity FORCE ROW LEVEL SECURITY;
ALTER TABLE kb.concept_similarity_build ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.concept_similarity_build FORCE ROW LEVEL SECURITY;

CREATE POLICY concept_embedding_read ON kb.concept_embedding FOR SELECT USING (
    kb.role_rank(kb.effective_role(kb.current_principal(),library_id)) >= 10
    AND EXISTS (
        SELECT 1 FROM kb.semantic_concept c
        WHERE c.id=concept_embedding.concept_id
          AND c.library_id=concept_embedding.library_id
          AND c.publication <> 'withdrawn'
    )
);
CREATE POLICY concept_embedding_worker_insert ON kb.concept_embedding FOR INSERT WITH CHECK (
    pg_has_role(current_user,'kb_worker','member')
    AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id)) >= 20
    AND kb.library_policy_allows(library_id,'external') IS TRUE
    AND EXISTS (
        SELECT 1 FROM kb.semantic_concept c
        WHERE c.id=concept_embedding.concept_id
          AND c.library_id=concept_embedding.library_id
          AND c.publication <> 'withdrawn'
    )
);
CREATE POLICY concept_similarity_read ON kb.concept_similarity FOR SELECT USING (
    kb.role_rank(kb.effective_role(kb.current_principal(),library_id)) >= 10
    AND EXISTS (
        SELECT 1 FROM kb.semantic_concept c
        WHERE c.id=concept_similarity.source_concept
          AND c.library_id=concept_similarity.library_id
          AND c.publication <> 'withdrawn'
    )
    AND EXISTS (
        SELECT 1 FROM kb.semantic_concept c
        WHERE c.id=concept_similarity.target_concept
          AND c.library_id=concept_similarity.library_id
          AND c.publication <> 'withdrawn'
    )
);
CREATE POLICY concept_similarity_worker_insert ON kb.concept_similarity FOR INSERT WITH CHECK (
    pg_has_role(current_user,'kb_worker','member')
    AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id)) >= 20
    AND kb.library_policy_allows(library_id,'external') IS TRUE
    AND EXISTS (
        SELECT 1 FROM kb.semantic_concept c
        WHERE c.id=concept_similarity.source_concept
          AND c.library_id=concept_similarity.library_id
          AND c.publication <> 'withdrawn'
    )
    AND EXISTS (
        SELECT 1 FROM kb.semantic_concept c
        WHERE c.id=concept_similarity.target_concept
          AND c.library_id=concept_similarity.library_id
          AND c.publication <> 'withdrawn'
    )
);

CREATE POLICY concept_similarity_build_read ON kb.concept_similarity_build FOR SELECT USING (
    kb.role_rank(kb.effective_role(kb.current_principal(),library_id)) >= 10
    AND NOT EXISTS (
        SELECT 1
        FROM unnest(concept_similarity_build.concept_ids) AS pinned(concept_id)
        WHERE NOT EXISTS (
            SELECT 1
            FROM kb.semantic_concept c
            WHERE c.id=pinned.concept_id
              AND c.library_id=concept_similarity_build.library_id
              AND c.publication <> 'withdrawn'
        )
    )
);
CREATE POLICY concept_similarity_build_worker_insert
ON kb.concept_similarity_build FOR INSERT WITH CHECK (
    pg_has_role(current_user,'kb_worker','member')
    AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id)) >= 20
    AND kb.library_policy_allows(library_id,'external') IS TRUE
    AND NOT EXISTS (
        SELECT 1
        FROM unnest(concept_similarity_build.concept_ids) AS pinned(concept_id)
        WHERE NOT EXISTS (
            SELECT 1
            FROM kb.semantic_concept c
            WHERE c.id=pinned.concept_id
              AND c.library_id=concept_similarity_build.library_id
              AND c.publication <> 'withdrawn'
        )
    )
);

CREATE FUNCTION kb.freeze_concept_similarity_cache() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'concept similarity cache is immutable'
        USING ERRCODE='check_violation';
    RETURN NULL;
END;
$$;
CREATE TRIGGER concept_embedding_frozen
    BEFORE UPDATE OR DELETE ON kb.concept_embedding
    FOR EACH ROW EXECUTE FUNCTION kb.freeze_concept_similarity_cache();
CREATE TRIGGER concept_similarity_frozen
    BEFORE UPDATE OR DELETE ON kb.concept_similarity
    FOR EACH ROW EXECUTE FUNCTION kb.freeze_concept_similarity_cache();
CREATE TRIGGER concept_similarity_build_frozen
    BEFORE UPDATE OR DELETE ON kb.concept_similarity_build
    FOR EACH ROW EXECUTE FUNCTION kb.freeze_concept_similarity_cache();

REVOKE ALL ON
    kb.concept_embedding,kb.concept_similarity,kb.concept_similarity_build
FROM PUBLIC;
GRANT SELECT(concept_id,library_id,profile_hash,text_hash,created_at)
    ON kb.concept_embedding TO kb_app;
GRANT SELECT ON kb.concept_similarity TO kb_app;
GRANT SELECT ON kb.concept_similarity_build TO kb_app;
GRANT SELECT,INSERT ON kb.concept_embedding,kb.concept_similarity TO kb_worker;
GRANT SELECT,INSERT ON kb.concept_similarity_build TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.valid_concept_embedding(double precision[],integer) TO kb_worker;
COMMIT;
