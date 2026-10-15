BEGIN;
ALTER TABLE kb.knowledge ADD COLUMN created_by uuid DEFAULT kb.current_principal();
ALTER TABLE kb.rule ADD COLUMN created_by uuid DEFAULT kb.current_principal();
-- Legacy drafts with unknown authors remain available to curators only.
CREATE FUNCTION kb.content_author_frozen() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,kb AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   IF OLD.publication='published' THEN
     RAISE EXCEPTION 'published knowledge is immutable' USING ERRCODE='check_violation';
   END IF;
   RETURN OLD;
 ELSIF TG_OP='INSERT' THEN
   IF kb.current_principal() IS NOT NULL AND NEW.created_by IS DISTINCT FROM kb.current_principal()
   THEN RAISE EXCEPTION 'forbidden' USING ERRCODE='insufficient_privilege'; END IF;
 ELSE
   IF NEW.created_by IS DISTINCT FROM OLD.created_by THEN
     RAISE EXCEPTION 'author is immutable' USING ERRCODE='check_violation';
   END IF;
   IF NEW.id IS DISTINCT FROM OLD.id OR NEW.library_id IS DISTINCT FROM OLD.library_id THEN
     RAISE EXCEPTION 'content identity is immutable' USING ERRCODE='check_violation';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER knowledge_author_frozen BEFORE INSERT OR UPDATE OR DELETE ON kb.knowledge
 FOR EACH ROW EXECUTE FUNCTION kb.content_author_frozen();
CREATE TRIGGER rule_author_frozen BEFORE INSERT OR UPDATE ON kb.rule
 FOR EACH ROW EXECUTE FUNCTION kb.content_author_frozen();

CREATE FUNCTION kb.content_dependencies_visible(p_kind text,p_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
 SELECT CASE p_kind WHEN 'knowledge' THEN
 NOT EXISTS(SELECT 1 FROM kb.knowledge_provenance p
   LEFT JOIN kb.source s ON s.id=p.source_id WHERE p.knowledge_id=p_id
   AND (s.id IS NULL OR s.publication='withdrawn'
      OR COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id)),0)<10))
 WHEN 'rule' THEN
 NOT EXISTS(SELECT 1 FROM kb.rule_provenance p
   LEFT JOIN kb.source s ON s.id=p.source_id WHERE p.rule_id=p_id
   AND (s.id IS NULL OR s.publication='withdrawn'
      OR COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id)),0)<10))
 ELSE false END
$$;
REVOKE ALL ON FUNCTION kb.content_dependencies_visible(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.content_dependencies_visible(text,uuid) TO kb_app,kb_worker,kb_provisioner;

CREATE POLICY knowledge_visibility ON kb.knowledge AS RESTRICTIVE FOR SELECT USING (
 (publication='published' OR created_by=kb.current_principal()
    OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30)
 AND kb.content_dependencies_visible('knowledge',id)
);
CREATE POLICY rule_visibility ON kb.rule AS RESTRICTIVE FOR SELECT USING (
 (publication='published' OR created_by=kb.current_principal()
    OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30)
 AND kb.content_dependencies_visible('rule',id)
);
DROP POLICY knowledge_write ON kb.knowledge;
DROP POLICY rule_write ON kb.rule;
CREATE POLICY knowledge_insert ON kb.knowledge FOR INSERT WITH CHECK (
 created_by=kb.current_principal()
 AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20);
CREATE POLICY rule_insert ON kb.rule FOR INSERT WITH CHECK (
 created_by=kb.current_principal()
 AND kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20);
CREATE POLICY knowledge_update ON kb.knowledge FOR UPDATE USING (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND (created_by=kb.current_principal()
 OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30))
 WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND (created_by=kb.current_principal()
 OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30));
CREATE POLICY rule_update ON kb.rule FOR UPDATE USING (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND (created_by=kb.current_principal()
 OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30))
 WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND (created_by=kb.current_principal()
 OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30));
CREATE POLICY knowledge_delete ON kb.knowledge FOR DELETE USING (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND (created_by=kb.current_principal()
 OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30));
CREATE POLICY rule_delete ON kb.rule FOR DELETE USING (
 kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=20
 AND (created_by=kb.current_principal()
 OR kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=30));
CREATE POLICY use_rule_visibility ON kb.use_record AS RESTRICTIVE FOR ALL USING (
 EXISTS(SELECT 1 FROM kb.rule r WHERE r.id=rule_id AND r.version_no=rule_version_no)
);
COMMIT;
