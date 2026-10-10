BEGIN;
ALTER TABLE kb.source ADD COLUMN author text, ADD COLUMN edition text,
 ADD COLUMN publication_ref text, ADD COLUMN origin_url text, ADD COLUMN retrieved_at timestamptz;
ALTER TABLE kb.fragment ADD COLUMN table_index integer CHECK(table_index>=1),
 ADD COLUMN cell_row integer CHECK(cell_row>=1), ADD COLUMN cell_column integer CHECK(cell_column>=1);
ALTER TABLE kb.job ADD COLUMN extraction_enabled boolean NOT NULL DEFAULT false,
 ADD COLUMN chunks_total integer, ADD COLUMN chunks_done integer NOT NULL DEFAULT 0;
ALTER TABLE kb.knowledge_provenance DROP CONSTRAINT knowledge_provenance_pkey;
ALTER TABLE kb.knowledge_provenance ADD COLUMN id uuid PRIMARY KEY DEFAULT gen_random_uuid();
ALTER TABLE kb.knowledge_provenance ADD UNIQUE NULLS NOT DISTINCT(knowledge_id,source_id,fragment_id);
CREATE TABLE kb.extraction_checkpoint (
 source_id uuid NOT NULL REFERENCES kb.source(id), chunk_no integer NOT NULL,
 content_hash text NOT NULL, model text NOT NULL, completed_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_id,chunk_no,content_hash,model)
);
ALTER TABLE kb.extraction_checkpoint ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.extraction_checkpoint FORCE ROW LEVEL SECURITY;
CREATE POLICY checkpoint_access ON kb.extraction_checkpoint FOR ALL USING(
 EXISTS(SELECT 1 FROM kb.source s WHERE s.id=source_id AND
 kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id))>=20)) WITH CHECK(
 EXISTS(SELECT 1 FROM kb.source s WHERE s.id=source_id AND
 kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id))>=20));
GRANT SELECT,INSERT ON kb.extraction_checkpoint TO kb_app,kb_worker;
GRANT SELECT ON kb.membership,kb.library,kb.organisation_admin,kb.generation_policy TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.is_org_admin(uuid,uuid),kb.membership_allows(uuid) TO kb_worker;
CREATE FUNCTION kb.finish_revoked_job(p_job uuid,p_actor uuid,p_account uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,kb AS $$
DECLARE target kb.job%ROWTYPE;
BEGIN
 IF NOT pg_has_role(session_user,'kb_worker','member') THEN
   RAISE EXCEPTION 'worker_only';
 END IF;
 SELECT * INTO target FROM kb.job WHERE id=p_job AND submitted_by=p_actor FOR UPDATE;
 IF NOT FOUND THEN RETURN; END IF;
 IF NOT EXISTS(SELECT 1 FROM kb.membership WHERE principal_id=p_actor
     AND organisation_id=p_account AND status='active')
    OR COALESCE(kb.role_rank(kb.effective_role(p_actor,target.library_id)),0)<20 THEN
    UPDATE kb.job SET status='failed',error_code='access_revoked',completed_at=now() WHERE id=p_job;
    UPDATE kb.source SET processing='partial' WHERE id=target.source_id AND processing='running';
 END IF;
END $$;
REVOKE ALL ON FUNCTION kb.finish_revoked_job(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.finish_revoked_job(uuid,uuid,uuid) TO kb_worker;
COMMIT;
