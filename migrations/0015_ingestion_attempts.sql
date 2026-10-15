BEGIN;
ALTER TABLE kb.job ADD COLUMN attempt_token uuid;
ALTER TABLE kb.job ADD COLUMN lease_until timestamptz;
DROP FUNCTION kb.finish_revoked_job(uuid,uuid,uuid);
CREATE FUNCTION kb.finish_revoked_job(p_job uuid,p_actor uuid,p_account uuid,p_attempt uuid)
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
    OR (target.extraction_enabled AND NOT kb.is_org_admin(p_account,p_actor)) THEN
    UPDATE kb.job SET status='failed',error_code='access_revoked',completed_at=now(),
        lease_until=NULL WHERE id=p_job AND attempt_token IS NOT DISTINCT FROM p_attempt;
    UPDATE kb.source SET processing='partial' WHERE id=target.source_id AND processing='running';
 END IF;
END $$;
REVOKE ALL ON FUNCTION kb.finish_revoked_job(uuid,uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.finish_revoked_job(uuid,uuid,uuid,uuid) TO kb_worker;
COMMIT;
