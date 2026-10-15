-- Library policy is independent of identity scopes and library role grants.
BEGIN;
CREATE TABLE kb.library_policy (
    library_id uuid PRIMARY KEY REFERENCES kb.library(id) ON DELETE CASCADE,
    external_processing_allowed boolean NOT NULL DEFAULT true,
    cloud_mcp_allowed boolean NOT NULL DEFAULT false,
    export_allowed boolean NOT NULL DEFAULT true
);
ALTER TABLE kb.library_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.library_policy FORCE ROW LEVEL SECURITY;
CREATE POLICY library_policy_read ON kb.library_policy FOR SELECT USING (
    kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=10
);
CREATE POLICY library_policy_write ON kb.library_policy FOR ALL USING (
    kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=40
) WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(),library_id))>=40);
GRANT SELECT,INSERT,UPDATE ON kb.library_policy TO kb_app;
GRANT SELECT ON kb.library_policy TO kb_worker,kb_provisioner;

CREATE FUNCTION kb.library_policy_allows(p_library uuid,p_purpose text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
 SELECT CASE p_purpose
 WHEN 'external' THEN COALESCE((SELECT external_processing_allowed FROM kb.library_policy
    WHERE library_id=p_library),true)
 WHEN 'cloud' THEN COALESCE((SELECT cloud_mcp_allowed AND external_processing_allowed
    FROM kb.library_policy WHERE library_id=p_library),false)
 WHEN 'export' THEN COALESCE((SELECT export_allowed FROM kb.library_policy
    WHERE library_id=p_library),true)
 WHEN 'generation' THEN COALESCE((SELECT generation_allowed FROM kb.generation_policy
    WHERE library_id=p_library),false) AND kb.library_policy_allows(p_library,'external')
 ELSE false END
$$;
REVOKE ALL ON FUNCTION kb.library_policy_allows(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.library_policy_allows(uuid,text) TO kb_app,kb_worker,kb_provisioner;

CREATE OR REPLACE FUNCTION kb.effective_role(p_principal uuid,p_library uuid)
RETURNS kb.library_role LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
 SELECT path.role FROM (
   SELECT g.role,0 AS direct FROM kb.library_grant g
    WHERE g.library_id=p_library AND g.principal_id=p_principal
   UNION ALL
   SELECT gg.role,1 AS direct FROM kb.access_group_grant gg
    JOIN kb.access_group_member m ON m.group_id=gg.group_id
    WHERE gg.library_id=p_library AND m.principal_id=p_principal
 ) path WHERE kb.membership_allows(p_principal)
 AND (COALESCE(current_setting('app.cloud_client',true),'')<>'true'
      OR kb.library_policy_allows(p_library,'cloud'))
 ORDER BY kb.role_rank(path.role) DESC,path.direct ASC LIMIT 1
$$;

CREATE OR REPLACE FUNCTION kb.research_generation_allowed(
    p_account uuid,p_library uuid,p_manifest jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
 SELECT COALESCE(
 p_account=nullif(current_setting('app.account',true),'')::uuid
 AND kb.is_org_admin(p_account,kb.current_principal())
 AND EXISTS(SELECT 1 FROM kb.membership m WHERE m.organisation_id=p_account
    AND m.principal_id=kb.current_principal() AND m.status='active')
 AND EXISTS(SELECT 1 FROM kb.library l WHERE l.id=p_library AND l.organisation_id=p_account
    AND kb.role_rank(kb.effective_role(kb.current_principal(),l.id))>=20)
 AND kb.library_policy_allows(p_library,'generation')
 AND kb.can_read_manifest(p_manifest)
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p_manifest->'sources') d
   LEFT JOIN kb.source s ON s.id=(d->>'source_id')::uuid
   LEFT JOIN kb.library l ON l.id=s.library_id
   WHERE s.id IS NULL OR l.id IS NULL OR l.organisation_id<>p_account
      OR NOT kb.library_policy_allows(s.library_id,'generation')
      OR COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id)),0)<20),false)
$$;

-- Policy changes invalidate semantic plans, including in-flight query hydration.
CREATE FUNCTION kb.policy_revision_changed() RETURNS trigger LANGUAGE plpgsql
SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
BEGIN
 UPDATE kb.library SET generation=generation+1 WHERE id=NEW.library_id;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION kb.policy_revision_changed() FROM PUBLIC;
CREATE TRIGGER library_policy_revision AFTER INSERT OR UPDATE ON kb.library_policy
 FOR EACH ROW EXECUTE FUNCTION kb.policy_revision_changed();

CREATE OR REPLACE FUNCTION kb.finish_revoked_job(p_job uuid,p_actor uuid,p_account uuid,p_attempt uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
DECLARE target kb.job%ROWTYPE;
BEGIN
 IF NOT pg_has_role(session_user,'kb_worker','member') THEN
   RAISE EXCEPTION 'worker_only';
 END IF;
 SELECT * INTO target FROM kb.job WHERE id=p_job AND submitted_by=p_actor
    AND ((p_attempt IS NULL AND attempt_token IS NULL AND status='queued')
      OR (attempt_token=p_attempt AND status='running')) FOR UPDATE;
 IF NOT FOUND THEN RETURN; END IF;
 IF NOT EXISTS(SELECT 1 FROM kb.membership WHERE principal_id=p_actor
     AND organisation_id=p_account AND status='active')
    OR COALESCE(kb.role_rank(kb.effective_role(p_actor,target.library_id)),0)<20
    OR (target.extraction_enabled AND (NOT kb.is_org_admin(p_account,p_actor)
       OR NOT kb.library_policy_allows(target.library_id,'generation')))
    OR EXISTS(SELECT 1 FROM kb.source WHERE id=target.source_id AND publication='withdrawn') THEN
    UPDATE kb.job SET status='failed',error_code='access_revoked',completed_at=now(),
        lease_until=NULL WHERE id=p_job AND attempt_token IS NOT DISTINCT FROM p_attempt;
    UPDATE kb.source SET processing='partial' WHERE id=target.source_id AND processing='running';
 END IF;
END $$;

CREATE OR REPLACE FUNCTION kb.finish_revoked_research(p_run uuid,p_actor uuid,p_account uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
DECLARE target kb.research_run%ROWTYPE;
BEGIN
 IF NOT pg_has_role(session_user,'kb_worker','member') THEN
  RAISE EXCEPTION 'worker_only' USING ERRCODE='insufficient_privilege';
 END IF;
 SELECT * INTO target FROM kb.research_run WHERE id=p_run AND principal_id=p_actor
   AND account_id=p_account AND status IN ('queued','running') FOR UPDATE;
 IF NOT FOUND THEN RETURN; END IF;
 IF NOT EXISTS(SELECT 1 FROM kb.membership WHERE principal_id=p_actor
     AND organisation_id=p_account AND status='active')
 OR NOT kb.is_org_admin(p_account,p_actor)
 OR COALESCE(kb.role_rank(kb.effective_role(p_actor,target.library_id)),0)<20
 OR NOT kb.library_policy_allows(target.library_id,'generation')
 OR NOT kb.can_read_manifest(target.manifest)
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(target.manifest->'sources') d
   LEFT JOIN kb.source s ON s.id=(d->>'source_id')::uuid
   WHERE s.id IS NULL OR s.publication='withdrawn'
     OR NOT kb.library_policy_allows(s.library_id,'generation')
     OR s.content_hash<>d->>'content_hash'
     OR COALESCE(kb.role_rank(kb.effective_role(p_actor,s.library_id)),0)<20) THEN
  UPDATE kb.research_run SET status='failed',error_code='access_revoked',completed_at=now()
   WHERE id=p_run;
 END IF;
END $$;
COMMIT;
