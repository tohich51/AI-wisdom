BEGIN;

ALTER TABLE kb.index_account
    ADD COLUMN profile_hash text,
    ADD CONSTRAINT index_account_embedding_profile_fk
        FOREIGN KEY (profile_hash, embedding_profile, dimension)
        REFERENCES kb.embedding_profile (profile_hash, name, dimension);

ALTER TABLE kb.index_generation
    ADD COLUMN profile_hash text,
    ADD COLUMN indexed_revision integer,
    ADD COLUMN manifest_hash text,
    ADD COLUMN resource_root text,
    ADD COLUMN indexed_documents integer,
    ADD COLUMN verified_at timestamptz,
    ADD CONSTRAINT index_generation_embedding_profile_fk
        FOREIGN KEY (profile_hash)
        REFERENCES kb.embedding_profile (profile_hash),
    ADD CONSTRAINT index_generation_projection_complete_chk CHECK (
        (
            profile_hash IS NULL
            AND indexed_revision IS NULL
            AND manifest_hash IS NULL
            AND resource_root IS NULL
            AND indexed_documents IS NULL
            AND verified_at IS NULL
        )
        OR
        (
            profile_hash IS NOT NULL
            AND indexed_revision IS NOT NULL
            AND indexed_revision >= 1
            AND manifest_hash IS NOT NULL
            AND manifest_hash ~ '^[0-9a-f]{64}$'
            AND resource_root IS NOT NULL
            AND resource_root ~ '^viking://resources/index/p-[0-9a-f]{64}/g-[1-9][0-9]*$'
            AND indexed_documents IS NOT NULL
            AND indexed_documents >= 0
        )
    );

ALTER TABLE kb.knowledge
    ADD CONSTRAINT knowledge_id_library_id_key UNIQUE (id, library_id);

CREATE TABLE kb.index_document (
    library_id uuid NOT NULL,
    generation integer NOT NULL,
    knowledge_id uuid NOT NULL,
    resource_uri text NOT NULL,
    statement_hash text NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT index_document_pkey
        PRIMARY KEY (library_id, generation, knowledge_id),
    CONSTRAINT index_document_library_generation_key
        UNIQUE (library_id, generation, resource_uri),
    CONSTRAINT index_document_statement_hash_chk
        CHECK (statement_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT index_document_resource_uri_chk
        CHECK (
            resource_uri ~ '^viking://resources/index/'
            AND position('..' IN resource_uri) = 0
            AND position('?' IN resource_uri) = 0
            AND position('#' IN resource_uri) = 0
        ),
    CONSTRAINT index_document_generation_fk
        FOREIGN KEY (library_id, generation)
        REFERENCES kb.index_generation (library_id, generation)
        ON DELETE CASCADE,
    CONSTRAINT index_document_knowledge_fk
        FOREIGN KEY (knowledge_id, library_id)
        REFERENCES kb.knowledge (id, library_id)
        ON DELETE CASCADE
);

ALTER TABLE kb.index_document ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.index_document FORCE ROW LEVEL SECURITY;

COMMIT;
