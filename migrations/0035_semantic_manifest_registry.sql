; -- Terminate the prior file for legacy concatenation runners.
-- 0035: kb.semantic_manifest registry — statement-local normalization of can_read_manifest facts.
-- Additive only: one new table, one new trigger function, two new triggers.
-- Facts only (library_id + manifest). No decisions, no timestamps, no retention jobs.
-- Authorization is recomputed per statement by existing RLS + kb.can_read_manifest().
-- Run inside the caller's transaction as a verified superuser. No BEGIN/COMMIT.

-- 1. Registry table: PK on (library_id, manifest_hash) only; no btree on full jsonb.
CREATE TABLE kb.semantic_manifest (
  library_id uuid NOT NULL REFERENCES kb.library (id),
  manifest jsonb NOT NULL,
  manifest_hash text GENERATED ALWAYS AS
    (encode(pg_catalog.sha256(pg_catalog.jsonb_send(manifest)), 'hex')) STORED,
  CONSTRAINT semantic_manifest_pkey PRIMARY KEY (library_id, manifest_hash)
);

-- 2. Privileges: SELECT + INSERT only, no UPDATE/DELETE, no PUBLIC.
REVOKE ALL ON kb.semantic_manifest FROM PUBLIC;
GRANT SELECT, INSERT ON kb.semantic_manifest TO kb_app, kb_worker;
ALTER TABLE kb.semantic_manifest ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.semantic_manifest FORCE ROW LEVEL SECURITY;

-- 3. Registry policies: same-account library + rank >= 10 + can_read_manifest, re-evaluated per statement.
CREATE POLICY semantic_manifest_read ON kb.semantic_manifest
  FOR SELECT
  USING (EXISTS (SELECT 1
                    FROM kb.library l
                   WHERE l.id = semantic_manifest.library_id
                     AND l.organisation_id = nullif(pg_catalog.current_setting('app.account', true), '')::uuid)
     AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10
     AND kb.can_read_manifest(manifest));

CREATE POLICY semantic_manifest_write ON kb.semantic_manifest
  FOR INSERT
  WITH CHECK (EXISTS (SELECT 1
                        FROM kb.library l
                       WHERE l.id = semantic_manifest.library_id
                         AND l.organisation_id = nullif(pg_catalog.current_setting('app.account', true), '')::uuid)
     AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10
     AND kb.can_read_manifest(manifest));

-- 4. Seed existing non-null manifests from both graph tables (superuser, before policy alteration).
INSERT INTO kb.semantic_manifest (library_id, manifest)
SELECT DISTINCT c.library_id, c.manifest
  FROM kb.semantic_concept c
 WHERE c.manifest IS NOT NULL
ON CONFLICT (library_id, manifest_hash) DO NOTHING;

INSERT INTO kb.semantic_manifest (library_id, manifest)
SELECT DISTINCT r.library_id, r.manifest
  FROM kb.semantic_relation r
 WHERE r.manifest IS NOT NULL
ON CONFLICT (library_id, manifest_hash) DO NOTHING;

ANALYZE kb.semantic_manifest;

-- 5. Replace the existing restrictive policies in place (no new policies on the original tables).
ALTER POLICY concept_dependencies ON kb.semantic_concept
  USING (CASE
    WHEN manifest IS NULL THEN true
    WHEN (library_id, manifest) IN (SELECT m.library_id, m.manifest FROM kb.semantic_manifest m) THEN true
    ELSE kb.can_read_manifest(manifest)
  END);

ALTER POLICY relation_dependencies ON kb.semantic_relation
  USING ((CASE
    WHEN manifest IS NULL THEN true
    WHEN (library_id, manifest) IN (SELECT m.library_id, m.manifest FROM kb.semantic_manifest m) THEN true
    ELSE kb.can_read_manifest(manifest)
  END)
    AND (EXISTS ( SELECT 1
   FROM kb.semantic_concept c
  WHERE ((c.id = semantic_relation.from_concept) AND (c.publication <> 'withdrawn'::text) AND (c.library_id = semantic_relation.library_id))))
    AND (EXISTS ( SELECT 1
   FROM kb.semantic_concept c
  WHERE ((c.id = semantic_relation.to_concept) AND (c.publication <> 'withdrawn'::text) AND (c.library_id = semantic_relation.library_id)))));

-- 6. Register manifests on write. SECURITY INVOKER, fixed search_path, fully qualified.
CREATE FUNCTION kb.semantic_manifest_register()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog, kb
AS $$
BEGIN
  IF NEW.manifest IS NOT NULL THEN
    INSERT INTO kb.semantic_manifest (library_id, manifest)
    VALUES (NEW.library_id, NEW.manifest)
    ON CONFLICT (library_id, manifest_hash) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION kb.semantic_manifest_register() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.semantic_manifest_register() TO kb_app, kb_worker;

CREATE TRIGGER semantic_manifest_concept_aiu
  BEFORE INSERT OR UPDATE OF manifest, library_id ON kb.semantic_concept
  FOR EACH ROW EXECUTE FUNCTION kb.semantic_manifest_register();

CREATE TRIGGER semantic_manifest_relation_aiu
  BEFORE INSERT OR UPDATE OF manifest, library_id ON kb.semantic_relation
  FOR EACH ROW EXECUTE FUNCTION kb.semantic_manifest_register();
