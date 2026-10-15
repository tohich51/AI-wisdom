BEGIN;

ALTER TABLE kb.outcome
 ADD COLUMN achievement text NOT NULL DEFAULT 'unknown'
   CHECK(achievement IN ('met','partly','not_met','unknown')),
 ADD COLUMN assessor_kind text NOT NULL DEFAULT 'unknown'
   CHECK(assessor_kind IN ('human','agent','measurement','unknown')),
 ADD COLUMN assessment_evidence text CHECK(char_length(assessment_evidence)<=4000),
 ADD CONSTRAINT measurement_has_evidence CHECK(
   assessor_kind<>'measurement' OR nullif(btrim(assessment_evidence),'') IS NOT NULL);

-- A delivered response is not evidence of reading, application or usefulness.
-- Item UUIDs are intentionally not cascading FKs: removing content never rewrites
-- a private issuance history; RLS hides entries whose dependencies are gone.
CREATE TABLE kb.retrieval_event (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 principal_id uuid NOT NULL,
 account_id uuid NOT NULL REFERENCES kb.organisation(id),
 library_id uuid NOT NULL REFERENCES kb.library(id),
 item_kind text NOT NULL CHECK(item_kind IN ('source','knowledge','rule')),
 item_id uuid NOT NULL,
 version_no integer,
 delivered_at timestamptz NOT NULL DEFAULT now(),
 CHECK((item_kind='rule' AND version_no IS NOT NULL AND version_no>=1) OR
       (item_kind IN ('source','knowledge') AND version_no IS NULL))
);
CREATE INDEX retrieval_event_personal ON kb.retrieval_event(principal_id,delivered_at DESC);
ALTER TABLE kb.retrieval_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.retrieval_event FORCE ROW LEVEL SECURITY;

CREATE FUNCTION kb.retrieval_target_visible(p_library uuid,p_kind text,p_item uuid,p_version integer)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
 SELECT CASE p_kind
 WHEN 'source' THEN EXISTS(SELECT 1 FROM kb.source WHERE id=p_item AND library_id=p_library)
 WHEN 'knowledge' THEN EXISTS(SELECT 1 FROM kb.knowledge WHERE id=p_item AND library_id=p_library)
 WHEN 'rule' THEN EXISTS(SELECT 1 FROM kb.rule WHERE id=p_item AND library_id=p_library
    AND version_no=p_version)
 ELSE false END
$$;
REVOKE ALL ON FUNCTION kb.retrieval_target_visible(uuid,text,uuid,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.retrieval_target_visible(uuid,text,uuid,integer) TO kb_app;
CREATE POLICY retrieval_personal_read ON kb.retrieval_event FOR SELECT USING(
 principal_id=kb.current_principal()
 AND account_id=nullif(current_setting('app.account',true),'')::uuid
 AND EXISTS(SELECT 1 FROM kb.library WHERE id=library_id AND organisation_id=account_id)
 AND kb.retrieval_target_visible(library_id,item_kind,item_id,version_no));
CREATE POLICY retrieval_personal_insert ON kb.retrieval_event FOR INSERT WITH CHECK(
 principal_id=kb.current_principal()
 AND account_id=nullif(current_setting('app.account',true),'')::uuid
 AND EXISTS(SELECT 1 FROM kb.library WHERE id=library_id AND organisation_id=account_id)
 AND kb.retrieval_target_visible(library_id,item_kind,item_id,version_no));
GRANT SELECT,INSERT ON kb.retrieval_event TO kb_app;
CREATE FUNCTION kb.immutable_retrieval_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable_retrieval_event'; END $$;
REVOKE ALL ON FUNCTION kb.immutable_retrieval_event() FROM PUBLIC;
CREATE TRIGGER retrieval_event_append_only BEFORE UPDATE OR DELETE ON kb.retrieval_event
 FOR EACH ROW EXECUTE FUNCTION kb.immutable_retrieval_event();

-- This security-definer sees org backlog hidden by ordinary content RLS, but
-- returns no counts or foreign identifiers. All submitters share these locks.
CREATE FUNCTION kb.check_processing_admission(p_actor uuid,p_account uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
DECLARE actor_count bigint; org_count bigint;
BEGIN
 IF p_actor IS DISTINCT FROM kb.current_principal()
 OR p_account IS DISTINCT FROM nullif(current_setting('app.account',true),'')::uuid
 OR NOT EXISTS(SELECT 1 FROM kb.membership WHERE principal_id=p_actor
    AND organisation_id=p_account AND status='active') THEN
   RAISE EXCEPTION 'forbidden' USING ERRCODE='insufficient_privilege';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('processing:org:'||p_account::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('processing:actor:'||p_actor::text,0));
 SELECT (SELECT count(*) FROM kb.job WHERE submitted_by=p_actor AND status IN ('queued','running'))
      + (SELECT count(*) FROM kb.research_run WHERE principal_id=p_actor AND status IN ('queued','running'))
 INTO actor_count;
 SELECT (SELECT count(*) FROM kb.job j JOIN kb.library l ON l.id=j.library_id
         WHERE l.organisation_id=p_account AND j.status IN ('queued','running'))
      + (SELECT count(*) FROM kb.research_run WHERE account_id=p_account AND status IN ('queued','running'))
 INTO org_count;
 IF actor_count>=3 OR org_count>=10 THEN
   RAISE EXCEPTION 'processing_queue_full' USING ERRCODE='check_violation';
 END IF;
END $$;
REVOKE ALL ON FUNCTION kb.check_processing_admission(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.check_processing_admission(uuid,uuid) TO kb_app;
COMMIT;
