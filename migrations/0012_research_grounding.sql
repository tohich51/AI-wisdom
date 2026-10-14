-- Additive selected-source research, private notes and dependency authority.
BEGIN;
SET search_path=kb,public;
-- Codex integration fallback: MiniMax G00 tasks produced no usable artifact.
CREATE FUNCTION kb.can_read_manifest(p_manifest jsonb) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
DECLARE item jsonb; source_ids uuid[]:='{}'; fragment_ids uuid[]:='{}';
 source_id uuid; version_id uuid; fragment_id uuid; start_at integer; end_at integer;
 visible_source uuid; visible_version uuid; source_hash text; version_hash text;
 publication text; canonical text; fragment_source uuid;
BEGIN
 IF jsonb_typeof(p_manifest) IS DISTINCT FROM 'object'
  OR jsonb_typeof(p_manifest->'sources') IS DISTINCT FROM 'array'
  OR jsonb_typeof(p_manifest->'fragments') IS DISTINCT FROM 'array' THEN RETURN false; END IF;
 IF jsonb_array_length(p_manifest->'sources') NOT BETWEEN 1 AND 10
  OR jsonb_array_length(p_manifest->'fragments') NOT BETWEEN 1 AND 24 THEN RETURN false; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(p_manifest->'sources') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object'
   OR jsonb_typeof(item->'source_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'source_version_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'content_hash') IS DISTINCT FROM 'string' THEN RETURN false; END IF;
  source_id:=(item->>'source_id')::uuid; version_id:=(item->>'source_version_id')::uuid;
  IF source_id IS NULL OR version_id IS NULL OR source_id=ANY(source_ids) THEN RETURN false; END IF;
  SELECT s.id,v.id,s.content_hash,v.content_hash,s.publication::text
  INTO visible_source,visible_version,source_hash,version_hash,publication
  FROM (SELECT source_id AS id) requested
  LEFT JOIN kb.source s ON s.id=requested.id
  LEFT JOIN kb.source_version v ON v.id=version_id AND v.source_id=s.id;
  IF visible_source IS NULL OR visible_version IS NULL OR publication='withdrawn'
    OR source_hash IS DISTINCT FROM item->>'content_hash'
    OR version_hash IS DISTINCT FROM item->>'content_hash' THEN RETURN false; END IF;
  source_ids:=array_append(source_ids,source_id);
 END LOOP;
 FOR item IN SELECT value FROM jsonb_array_elements(p_manifest->'fragments') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object'
   OR jsonb_typeof(item->'fragment_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'source_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'text_hash') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'excerpt_start') IS DISTINCT FROM 'number'
   OR jsonb_typeof(item->'excerpt_end') IS DISTINCT FROM 'number' THEN RETURN false; END IF;
  fragment_id:=(item->>'fragment_id')::uuid; source_id:=(item->>'source_id')::uuid;
  start_at:=(item->>'excerpt_start')::integer; end_at:=(item->>'excerpt_end')::integer;
  IF fragment_id IS NULL OR source_id IS NULL OR fragment_id=ANY(fragment_ids)
   OR NOT source_id=ANY(source_ids) OR start_at IS NULL OR end_at IS NULL THEN RETURN false; END IF;
  SELECT f.text,f.source_id INTO canonical,fragment_source
  FROM (SELECT fragment_id AS id) requested LEFT JOIN kb.fragment f ON f.id=requested.id;
  IF canonical IS NULL OR fragment_source IS DISTINCT FROM source_id
   OR start_at<0 OR end_at<=start_at OR end_at>char_length(canonical)
   OR encode(sha256(convert_to(canonical,'UTF8')),'hex') IS DISTINCT FROM item->>'text_hash'
   THEN RETURN false; END IF;
  fragment_ids:=array_append(fragment_ids,fragment_id);
 END LOOP;
 -- Every selected source must contribute a frozen excerpt.
 IF EXISTS(SELECT 1 FROM unnest(source_ids) selected WHERE NOT EXISTS(
    SELECT 1 FROM jsonb_array_elements(p_manifest->'fragments') f
    WHERE (f->>'source_id')::uuid=selected)) THEN RETURN false; END IF;
 RETURN true;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN false;
END $$;
REVOKE ALL ON FUNCTION kb.can_read_manifest(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.can_read_manifest(jsonb) TO kb_app,kb_worker;


CREATE TABLE kb.research_run (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 account_id uuid NOT NULL REFERENCES kb.organisation(id), principal_id uuid NOT NULL,
 library_id uuid NOT NULL REFERENCES kb.library(id), source_ids uuid[] NOT NULL,
 manifest jsonb NOT NULL,
 kind text NOT NULL CHECK(kind IN ('research','summary','faq','comparison','checklist','flashcards','topic','network')),
 query text NOT NULL DEFAULT '' CHECK(length(query)<=2000),
 idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
 request_hash text NOT NULL CHECK(request_hash ~ '^[0-9a-f]{64}$'),
 status text NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','done','failed')),
 attempt_token uuid, queue_id bigint, result jsonb, error_code text,
 created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, completed_at timestamptz,
 UNIQUE(principal_id,idempotency_key),
 CHECK(cardinality(source_ids) BETWEEN 1 AND 10),
 CHECK(status<>'done' OR (result IS NOT NULL AND jsonb_typeof(result)='object'))
);
CREATE INDEX research_owner_recent ON kb.research_run(principal_id,created_at DESC);

CREATE FUNCTION kb.research_generation_allowed(p_account uuid,p_library uuid,p_manifest jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
 SELECT COALESCE(
 p_account=nullif(current_setting('app.account',true),'')::uuid
 AND kb.is_org_admin(p_account,kb.current_principal())
 AND EXISTS(SELECT 1 FROM kb.membership m WHERE m.organisation_id=p_account
    AND m.principal_id=kb.current_principal() AND m.status='active')
 AND EXISTS(SELECT 1 FROM kb.library l WHERE l.id=p_library AND l.organisation_id=p_account
    AND kb.role_rank(kb.effective_role(kb.current_principal(),l.id))>=20)
 AND kb.can_read_manifest(p_manifest)
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p_manifest->'sources') d
   LEFT JOIN kb.source s ON s.id=(d->>'source_id')::uuid
   LEFT JOIN kb.library l ON l.id=s.library_id
   WHERE s.id IS NULL OR l.id IS NULL OR l.organisation_id<>p_account
      OR COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id)),0)<20),false)
$$;
REVOKE ALL ON FUNCTION kb.research_generation_allowed(uuid,uuid,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.research_generation_allowed(uuid,uuid,jsonb) TO kb_app,kb_worker;

ALTER TABLE kb.research_run ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.research_run FORCE ROW LEVEL SECURITY;
CREATE POLICY research_read ON kb.research_run FOR SELECT USING (
 principal_id=kb.current_principal() AND account_id=nullif(current_setting('app.account',true),'')::uuid
 AND EXISTS(SELECT 1 FROM kb.library l WHERE l.id=library_id AND l.organisation_id=account_id
    AND kb.role_rank(kb.effective_role(kb.current_principal(),l.id))>=10)
);
CREATE POLICY research_dependencies ON kb.research_run AS RESTRICTIVE FOR SELECT
 USING(kb.can_read_manifest(manifest));
CREATE POLICY research_insert ON kb.research_run FOR INSERT WITH CHECK (
 principal_id=kb.current_principal() AND status='queued' AND result IS NULL
 AND kb.research_generation_allowed(account_id,library_id,manifest));
CREATE POLICY research_update ON kb.research_run FOR UPDATE USING (
 principal_id=kb.current_principal() AND kb.research_generation_allowed(account_id,library_id,manifest))
 WITH CHECK(principal_id=kb.current_principal() AND kb.research_generation_allowed(account_id,library_id,manifest));
CREATE POLICY research_update_dependencies ON kb.research_run AS RESTRICTIVE FOR UPDATE
 USING(kb.can_read_manifest(manifest)) WITH CHECK(kb.can_read_manifest(manifest));
GRANT SELECT,INSERT,UPDATE ON kb.research_run TO kb_app;
GRANT SELECT,UPDATE ON kb.research_run TO kb_worker;

CREATE FUNCTION kb.freeze_research_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,kb AS $$
DECLARE expected uuid[];
BEGIN
 SELECT array_agg((d->>'source_id')::uuid ORDER BY (d->>'source_id')::uuid)
 INTO expected FROM jsonb_array_elements(NEW.manifest->'sources') d;
 IF expected IS NULL OR expected IS DISTINCT FROM
    ARRAY(SELECT x FROM unnest(NEW.source_ids) x ORDER BY x) THEN
   RAISE EXCEPTION 'invalid source manifest' USING ERRCODE='check_violation';
 END IF;
 IF TG_OP='UPDATE' THEN
  IF ROW(NEW.id,NEW.account_id,NEW.principal_id,NEW.library_id,NEW.source_ids,
         NEW.manifest,NEW.kind,NEW.query,NEW.idempotency_key,NEW.request_hash,NEW.created_at)
     IS DISTINCT FROM
     ROW(OLD.id,OLD.account_id,OLD.principal_id,OLD.library_id,OLD.source_ids,
         OLD.manifest,OLD.kind,OLD.query,OLD.idempotency_key,OLD.request_hash,OLD.created_at) THEN
   RAISE EXCEPTION 'research identity is immutable' USING ERRCODE='check_violation';
  END IF;
  IF OLD.status IN ('done','failed') AND NEW IS DISTINCT FROM OLD THEN
   RAISE EXCEPTION 'research result is immutable' USING ERRCODE='check_violation';
  END IF;
  IF NEW.status<>OLD.status AND NOT
    ((OLD.status='queued' AND NEW.status IN ('running','failed')) OR
     (OLD.status='running' AND NEW.status IN ('done','failed'))) THEN
   RAISE EXCEPTION 'invalid research transition' USING ERRCODE='check_violation';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER research_identity_frozen BEFORE INSERT OR UPDATE ON kb.research_run
 FOR EACH ROW EXECUTE FUNCTION kb.freeze_research_identity();

CREATE TABLE kb.research_note (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 account_id uuid NOT NULL REFERENCES kb.organisation(id),principal_id uuid NOT NULL,
 research_run_id uuid NOT NULL REFERENCES kb.research_run(id),
 title text NOT NULL CHECK(length(title) BETWEEN 1 AND 500),
 comment text NOT NULL DEFAULT '' CHECK(length(comment)<=8000),
 revision integer NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE kb.research_note ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.research_note FORCE ROW LEVEL SECURITY;
CREATE POLICY note_read ON kb.research_note FOR SELECT USING (
 principal_id=kb.current_principal() AND account_id=nullif(current_setting('app.account',true),'')::uuid);
CREATE POLICY note_parent_read ON kb.research_note AS RESTRICTIVE FOR SELECT USING (
 EXISTS(SELECT 1 FROM kb.research_run r WHERE r.id=research_run_id
   AND r.status='done' AND r.principal_id=research_note.principal_id
   AND r.account_id=research_note.account_id));
CREATE POLICY note_insert ON kb.research_note FOR INSERT WITH CHECK (
 principal_id=kb.current_principal() AND account_id=nullif(current_setting('app.account',true),'')::uuid
 AND EXISTS(SELECT 1 FROM kb.research_run r WHERE r.id=research_run_id AND r.status='done'
   AND r.principal_id=research_note.principal_id AND r.account_id=research_note.account_id));
CREATE POLICY note_update ON kb.research_note FOR UPDATE USING (
 principal_id=kb.current_principal() AND account_id=nullif(current_setting('app.account',true),'')::uuid
 AND EXISTS(SELECT 1 FROM kb.research_run r WHERE r.id=research_run_id AND r.status='done'))
 WITH CHECK(principal_id=kb.current_principal() AND account_id=nullif(current_setting('app.account',true),'')::uuid
 AND EXISTS(SELECT 1 FROM kb.research_run r WHERE r.id=research_run_id AND r.status='done'));
GRANT SELECT,INSERT,UPDATE ON kb.research_note TO kb_app;
CREATE FUNCTION kb.freeze_note_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.id,NEW.account_id,NEW.principal_id,NEW.research_run_id,NEW.created_at)
   IS DISTINCT FROM ROW(OLD.id,OLD.account_id,OLD.principal_id,OLD.research_run_id,OLD.created_at) THEN
  RAISE EXCEPTION 'note identity is immutable' USING ERRCODE='check_violation';
 END IF;
 NEW.revision:=OLD.revision+1; NEW.updated_at:=now(); RETURN NEW;
END $$;
CREATE TRIGGER note_identity_frozen BEFORE UPDATE ON kb.research_note
 FOR EACH ROW EXECUTE FUNCTION kb.freeze_note_identity();

CREATE FUNCTION kb.finish_revoked_research(p_run uuid,p_actor uuid,p_account uuid)
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
 OR NOT kb.can_read_manifest(target.manifest)
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(target.manifest->'sources') d
   LEFT JOIN kb.source s ON s.id=(d->>'source_id')::uuid
   WHERE s.id IS NULL OR s.publication='withdrawn'
     OR s.content_hash<>d->>'content_hash'
     OR COALESCE(kb.role_rank(kb.effective_role(p_actor,s.library_id)),0)<20) THEN
  UPDATE kb.research_run SET status='failed',error_code='access_revoked',completed_at=now()
   WHERE id=p_run;
 END IF;
END $$;
REVOKE ALL ON FUNCTION kb.finish_revoked_research(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.finish_revoked_research(uuid,uuid,uuid) TO kb_worker;

CREATE FUNCTION kb.freeze_source_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'source version is immutable' USING ERRCODE='check_violation';
END $$;
CREATE TRIGGER source_version_frozen BEFORE UPDATE ON kb.source_version
 FOR EACH ROW EXECUTE FUNCTION kb.freeze_source_version();
COMMIT;
