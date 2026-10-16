BEGIN;

-- This expression is the same regconfig overload used by lexical search.
-- Additive only: query predicates, ranking, ACL and publication checks stay in code.
CREATE INDEX knowledge_statement_simple_fts_gin
    ON kb.knowledge
    USING gin (to_tsvector('simple', statement));

COMMIT;
