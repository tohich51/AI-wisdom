BEGIN;
-- Explicit immutable publications. Private use/outcome RLS is unchanged;
-- later private corrections never become shared without a new publication.
CREATE TABLE kb.shared_experience (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 library_id uuid NOT NULL REFERENCES kb.library(id),
 use_id uuid NOT NULL REFERENCES kb.use_record(id),
 outcome_id uuid NOT NULL REFERENCES kb.outcome(id),
 rule_id uuid NOT NULL,
 rule_version_no integer NOT NULL,
 publisher_id uuid NOT NULL,
 snapshot jsonb NOT NULL CHECK(jsonb_typeof(snapshot)='object'),
 published_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(rule_id,rule_version_no) REFERENCES kb.rule(id,version_no),
 UNIQUE(library_id,use_id,outcome_id)
);
CREATE INDEX shared_experience_library ON kb.shared_experience(library_id,published_at DESC);
ALTER TABLE kb.shared_experience ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.shared_experience FORCE ROW LEVEL SECURITY;
CREATE FUNCTION kb.experience_snapshot(p_use uuid,p_outcome uuid) RETURNS jsonb
 LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
 SELECT jsonb_build_object(
  'use_id',u.id,'outcome_id',o.id,'rule_id',u.rule_id,
  'rule_version_no',u.rule_version_no,'rule_title',r.title,
  'expected_result',e.expected_result,'where_applied',e.where_applied,
  'observed_result',o.observed_result,'rating',o.rating,'rating_basis',o.rating_basis,
  'achievement',o.achievement,'assessor_kind',o.assessor_kind,
  'assessment_evidence',o.assessment_evidence,'reported_at',o.reported_at,
  'is_correction',o.is_correction)
 FROM kb.use_record u JOIN kb.experience e ON e.use_id=u.id
 JOIN kb.outcome o ON o.use_id=u.id
 JOIN kb.rule r ON r.id=u.rule_id AND r.version_no=u.rule_version_no
 WHERE u.id=p_use AND o.id=p_outcome AND u.principal_id=kb.current_principal()
  AND o.reported_by=kb.current_principal() AND u.state<>'retrieved'
  AND r.publication='published'
$$;
REVOKE ALL ON FUNCTION kb.experience_snapshot(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.experience_snapshot(uuid,uuid) TO kb_app;
CREATE POLICY shared_experience_read ON kb.shared_experience FOR SELECT USING(
 EXISTS(SELECT 1 FROM kb.library l WHERE l.id=shared_experience.library_id
   AND l.kind IN('project','experience')
   AND l.organisation_id=nullif(current_setting('app.account',true),'')::uuid)
 AND EXISTS(SELECT 1 FROM kb.rule r WHERE r.id=shared_experience.rule_id AND r.version_no=shared_experience.rule_version_no
   AND r.publication='published'));
CREATE POLICY shared_experience_insert ON kb.shared_experience FOR INSERT WITH CHECK(
 publisher_id=kb.current_principal()
 AND snapshot IS NOT NULL AND snapshot=kb.experience_snapshot(use_id,outcome_id)
 AND snapshot->>'rule_id'=rule_id::text
 AND (snapshot->>'rule_version_no')::integer=rule_version_no
 AND EXISTS(SELECT 1 FROM kb.library target JOIN kb.rule r ON r.id=shared_experience.rule_id
   JOIN kb.library origin ON origin.id=r.library_id
   WHERE target.id=shared_experience.library_id AND target.kind IN('project','experience')
    AND target.organisation_id=origin.organisation_id
    AND target.organisation_id=nullif(current_setting('app.account',true),'')::uuid
    AND kb.role_rank(kb.effective_role(kb.current_principal(),target.id))>=30));
GRANT SELECT,INSERT ON kb.shared_experience TO kb_app;
CREATE FUNCTION kb.immutable_shared_experience() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'shared_experience_is_an_immutable_publication'; END $$;
REVOKE ALL ON FUNCTION kb.immutable_shared_experience() FROM PUBLIC;
CREATE TRIGGER shared_experience_append_only BEFORE UPDATE OR DELETE ON kb.shared_experience
 FOR EACH ROW EXECUTE FUNCTION kb.immutable_shared_experience();

-- Automatically record delivery of the explicit published copy as its own kind.
ALTER TABLE kb.retrieval_event DROP CONSTRAINT retrieval_event_item_kind_check;
ALTER TABLE kb.retrieval_event ADD CONSTRAINT retrieval_event_item_kind_check
 CHECK(item_kind IN('source','knowledge','rule','experience'));
ALTER TABLE kb.retrieval_event DROP CONSTRAINT retrieval_event_check;
ALTER TABLE kb.retrieval_event ADD CONSTRAINT retrieval_event_check
 CHECK((item_kind='rule' AND version_no IS NOT NULL AND version_no>=1) OR
       (item_kind IN('source','knowledge','experience') AND version_no IS NULL));
CREATE OR REPLACE FUNCTION kb.retrieval_target_visible(p_library uuid,p_kind text,p_item uuid,p_version integer)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
 SELECT CASE p_kind
 WHEN 'source' THEN EXISTS(SELECT 1 FROM kb.source WHERE id=p_item AND library_id=p_library)
 WHEN 'knowledge' THEN EXISTS(SELECT 1 FROM kb.knowledge WHERE id=p_item AND library_id=p_library)
 WHEN 'rule' THEN EXISTS(SELECT 1 FROM kb.rule WHERE id=p_item AND library_id=p_library
    AND version_no=p_version)
 WHEN 'experience' THEN EXISTS(SELECT 1 FROM kb.shared_experience WHERE id=p_item AND library_id=p_library)
 ELSE false END
$$;
COMMIT;
