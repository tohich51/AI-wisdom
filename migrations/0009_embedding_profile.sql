BEGIN;

CREATE TABLE kb.embedding_profile (
    profile_hash text PRIMARY KEY CHECK (profile_hash ~ '^[0-9a-f]{64}$'),
    name text NOT NULL,
    dimension integer NOT NULL CHECK (dimension > 0),
    canonical_config text NOT NULL,
    config jsonb NOT NULL,
    UNIQUE (profile_hash, name, dimension),
    CHECK (config = canonical_config::jsonb),
    CHECK (profile_hash = encode(sha256(convert_to(canonical_config, 'UTF8')), 'hex'))
);

INSERT INTO kb.embedding_profile (profile_hash, name, dimension, canonical_config, config)
SELECT '553c6c1b269dfb4933851f023e7266a805bc73b0c6b0f32f77357d7c33c4d147', 'openrouter:qwen3-embedding-8b:4096:v1', 4096, canonical_config, canonical_config::jsonb
FROM (VALUES ('{"api_base":"https://openrouter.ai/api/v1","chunking_policy":"published-knowledge-markdown:v1","dimension":4096,"model":"qwen/qwen3-embedding-8b","name":"openrouter:qwen3-embedding-8b:4096:v1","normalization":"provider_native","processor_version":"OpenViking0.5.0-vectors_only","provider":"openai","query_instruction":"Given a web search query, retrieve relevant passages that answer the query"}'::text)) AS c(canonical_config);

CREATE FUNCTION kb.embedding_profile_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'embedding_profile is immutable';
    RETURN NULL;
END;
$$;

CREATE TRIGGER embedding_profile_reject_mutation
BEFORE UPDATE OR DELETE ON kb.embedding_profile
FOR EACH ROW EXECUTE FUNCTION kb.embedding_profile_reject_mutation();

GRANT SELECT ON TABLE kb.embedding_profile TO kb_app, kb_provisioner;
REVOKE INSERT, UPDATE, DELETE ON TABLE kb.embedding_profile FROM kb_app, kb_worker, kb_provisioner, PUBLIC;

COMMIT;
