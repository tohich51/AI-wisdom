-- Preserve actual parser origin and structural sections for newly parsed fragments.
-- Unknown legacy metadata stays NULL; no defaults, backfill or reparsing.
ALTER TABLE kb.fragment
    ADD COLUMN parser_name text,
    ADD COLUMN parser_version text,
    ADD COLUMN spine text,
    ADD COLUMN heading text;
