-- Library-scoped semantic drafts; original evidence stays PostgreSQL authority.
BEGIN;
CREATE TABLE kb.semantic_concept (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), library_id uuid NOT NULL REFERENCES kb.library,
 principal_id uuid NOT NULL, lineage_id uuid NOT NULL, version_no integer NOT NULL CHECK(version_no>0),
 label text NOT NULL CHECK(length(label) BETWEEN 1 AND 500), body text NOT NULL CHECK(length(body) BETWEEN 1 AND 2000),
 publication text NOT NULL DEFAULT 'draft' CHECK(publication IN ('draft','published','withdrawn')),
 origin text NOT NULL CHECK(origin IN ('manual','generated','manual_revision')), origin_run uuid REFERENCES kb.research_run,
 origin_item integer, manifest jsonb, citations jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(lineage_id,version_no),UNIQUE(origin_run,origin_item),
 CHECK((origin='manual' AND manifest IS NULL AND origin_run IS NULL) OR
       (origin='generated' AND manifest IS NOT NULL AND origin_run IS NOT NULL AND origin_item IS NOT NULL) OR
       (origin='manual_revision' AND manifest IS NOT NULL AND origin_run IS NULL AND origin_item IS NULL))
);
CREATE TABLE kb.semantic_relation (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), library_id uuid NOT NULL REFERENCES kb.library,
 principal_id uuid NOT NULL, lineage_id uuid NOT NULL, version_no integer NOT NULL CHECK(version_no>0),
 from_concept uuid NOT NULL REFERENCES kb.semantic_concept, to_concept uuid NOT NULL REFERENCES kb.semantic_concept,
 predicate text NOT NULL CHECK(predicate IN ('supports','part_of','used_for','contradicts')),
 hypothesis boolean NOT NULL DEFAULT true,
 publication text NOT NULL DEFAULT 'draft' CHECK(publication IN ('draft','published','withdrawn')),
 origin text NOT NULL CHECK(origin IN ('manual','generated','manual_revision')),origin_run uuid REFERENCES kb.research_run,
 origin_item integer, manifest jsonb,citations jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(lineage_id,version_no),UNIQUE(origin_run,origin_item),
 CHECK(from_concept<>to_concept),
 CHECK((origin='manual' AND manifest IS NULL AND origin_run IS NULL) OR
       (origin='generated' AND manifest IS NOT NULL AND origin_run IS NOT NULL AND origin_item IS NOT NULL) OR
       (origin='manual_revision' AND manifest IS NOT NULL AND origin_run IS NULL AND origin_item IS NULL))
);
ALTER TABLE kb.semantic_concept ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.semantic_concept FORCE ROW LEVEL SECURITY;
ALTER TABLE kb.semantic_relation ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.semantic_relation FORCE ROW LEVEL SECURITY;
CREATE POLICY concept_read ON kb.semantic_concept FOR SELECT USING (
 (publication<>'withdrawn' OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30)
 AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=10
 AND (publication='published' OR principal_id=kb.current_principal())
 AND EXISTS(SELECT 1 FROM kb.library l WHERE l.id=library_id
   AND l.organisation_id=nullif(current_setting('app.account',true),'')::uuid));
CREATE POLICY concept_dependencies ON kb.semantic_concept AS RESTRICTIVE FOR SELECT
 USING(manifest IS NULL OR kb.can_read_manifest(manifest));
CREATE POLICY concept_insert ON kb.semantic_concept FOR INSERT WITH CHECK (
 principal_id=kb.current_principal() AND publication='draft'
 AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND EXISTS(SELECT 1 FROM kb.library l WHERE l.id=library_id
   AND l.organisation_id=nullif(current_setting('app.account',true),'')::uuid)
 AND (manifest IS NULL OR kb.can_read_manifest(manifest)));
CREATE POLICY concept_update ON kb.semantic_concept FOR UPDATE USING (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30
 AND (manifest IS NULL OR kb.can_read_manifest(manifest))) WITH CHECK (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30
 AND (manifest IS NULL OR kb.can_read_manifest(manifest)));
CREATE POLICY relation_read ON kb.semantic_relation FOR SELECT USING (
 (publication<>'withdrawn' OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30)
 AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=10
 AND (publication='published' OR principal_id=kb.current_principal())
 AND EXISTS(SELECT 1 FROM kb.library l WHERE l.id=library_id
   AND l.organisation_id=nullif(current_setting('app.account',true),'')::uuid));
CREATE POLICY relation_dependencies ON kb.semantic_relation AS RESTRICTIVE FOR SELECT USING (
 (manifest IS NULL OR kb.can_read_manifest(manifest))
 AND EXISTS(SELECT 1 FROM kb.semantic_concept c WHERE c.id=from_concept AND c.publication<>'withdrawn' AND c.library_id=semantic_relation.library_id)
 AND EXISTS(SELECT 1 FROM kb.semantic_concept c WHERE c.id=to_concept AND c.publication<>'withdrawn' AND c.library_id=semantic_relation.library_id));
CREATE POLICY relation_insert ON kb.semantic_relation FOR INSERT WITH CHECK (
 principal_id=kb.current_principal() AND publication='draft'
 AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND (manifest IS NULL OR kb.can_read_manifest(manifest))
 AND EXISTS(SELECT 1 FROM kb.semantic_concept c WHERE c.id=from_concept AND c.publication<>'withdrawn' AND c.library_id=semantic_relation.library_id)
 AND EXISTS(SELECT 1 FROM kb.semantic_concept c WHERE c.id=to_concept AND c.publication<>'withdrawn' AND c.library_id=semantic_relation.library_id));
CREATE POLICY relation_update ON kb.semantic_relation FOR UPDATE USING (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30
 AND (manifest IS NULL OR kb.can_read_manifest(manifest))) WITH CHECK (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30
 AND (manifest IS NULL OR kb.can_read_manifest(manifest))
 AND EXISTS(SELECT 1 FROM kb.semantic_concept c WHERE c.id=from_concept AND c.publication<>'withdrawn' AND c.library_id=semantic_relation.library_id)
 AND EXISTS(SELECT 1 FROM kb.semantic_concept c WHERE c.id=to_concept AND c.publication<>'withdrawn' AND c.library_id=semantic_relation.library_id));
CREATE FUNCTION kb.freeze_semantic_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='UPDATE' THEN
  IF (to_jsonb(NEW)-'publication') IS DISTINCT FROM (to_jsonb(OLD)-'publication') THEN
    RAISE EXCEPTION 'semantic version is immutable' USING ERRCODE='check_violation';
  END IF;
  IF OLD.publication='withdrawn' OR (OLD.publication='published' AND NEW.publication<>'withdrawn') THEN
    RAISE EXCEPTION 'invalid semantic transition' USING ERRCODE='check_violation';
  END IF;
 END IF;
 IF NEW.publication<>'draft' AND (pg_has_role(current_user,'kb_worker','member') OR
    COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(),NEW.library_id)),0)<30) THEN
    RAISE EXCEPTION 'curator required' USING ERRCODE='insufficient_privilege';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER concept_frozen BEFORE INSERT OR UPDATE ON kb.semantic_concept
 FOR EACH ROW EXECUTE FUNCTION kb.freeze_semantic_record();
CREATE TRIGGER relation_frozen BEFORE INSERT OR UPDATE ON kb.semantic_relation
 FOR EACH ROW EXECUTE FUNCTION kb.freeze_semantic_record();
GRANT SELECT,INSERT,UPDATE ON kb.semantic_concept,kb.semantic_relation TO kb_app;
GRANT SELECT,INSERT ON kb.semantic_concept,kb.semantic_relation TO kb_worker;
COMMIT;
