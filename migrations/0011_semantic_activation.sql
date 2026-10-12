-- Vector projections reuse the existing account/generation registry.
-- Canonical content, ACL and activation remain PostgreSQL decisions.
BEGIN;

GRANT SELECT ON kb.knowledge, kb.membership, kb.library, kb.source, kb.knowledge_provenance TO kb_provisioner;
GRANT EXECUTE ON FUNCTION kb.is_org_admin(uuid,uuid) TO kb_provisioner;
GRANT SELECT ON kb.index_document TO kb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb.index_document TO kb_provisioner;

CREATE POLICY semantic_document_read ON kb.index_document FOR SELECT
TO kb_app, kb_provisioner
USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10);
CREATE POLICY semantic_document_write ON kb.index_document FOR ALL
TO kb_provisioner
USING (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40)
WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40);

CREATE POLICY semantic_generation_read ON kb.index_generation FOR SELECT
TO kb_app, kb_provisioner
USING (profile_hash IS NOT NULL
       AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 10);
CREATE POLICY semantic_generation_admit ON kb.index_generation FOR INSERT
TO kb_provisioner
WITH CHECK (profile_hash IS NOT NULL AND started_by = kb.current_principal()
       AND kb.role_rank(kb.effective_role(kb.current_principal(), library_id)) >= 40);

-- This lock is shared by content-revision changes and vector activation.
-- Indexer needs no UPDATE privilege on canonical library/content records.
CREATE FUNCTION kb.bump_semantic_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, kb AS $$
BEGIN
    IF TG_OP <> 'INSERT' AND OLD.publication = 'published' THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(OLD.library_id::text, 91));
        UPDATE kb.library SET generation = generation + 1 WHERE id = OLD.library_id;
    END IF;
    IF TG_OP <> 'DELETE' AND NEW.publication = 'published'
       AND (TG_OP = 'INSERT' OR OLD.publication <> 'published'
            OR OLD.library_id IS DISTINCT FROM NEW.library_id) THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.library_id::text, 91));
        UPDATE kb.library SET generation = generation + 1 WHERE id = NEW.library_id;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER knowledge_semantic_revision
AFTER INSERT OR UPDATE OR DELETE ON kb.knowledge
FOR EACH ROW EXECUTE FUNCTION kb.bump_semantic_revision();
REVOKE ALL ON FUNCTION kb.bump_semantic_revision() FROM PUBLIC;

CREATE FUNCTION kb.guard_semantic_generation()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, kb, public AS $$
DECLARE
    current_revision integer;
    mapped_count integer;
    published_count integer;
    actual_manifest text;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.profile_hash IS DISTINCT FROM NEW.profile_hash THEN
        RAISE EXCEPTION 'projection_profile_is_immutable';
    END IF;
    IF NEW.profile_hash IS NULL THEN RETURN NEW; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(NEW.library_id::text, 91));
    IF NOT pg_has_role(current_user, 'kb_provisioner', 'member') THEN
        RAISE EXCEPTION 'projection_writer_required' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF (COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(), NEW.library_id)),0) < 40
        OR NOT EXISTS(SELECT 1 FROM kb.library l JOIN kb.membership m
                      ON m.organisation_id=l.organisation_id
                      WHERE l.id=NEW.library_id AND m.principal_id=kb.current_principal()
                        AND m.status='active'))
       AND NOT(TG_OP='UPDATE' AND OLD.state='building' AND NEW.state='retired'
               AND OLD.started_by=kb.current_principal()
               AND pg_has_role(session_user,'kb_provisioner','member'))
       OR (TG_OP='INSERT' AND NEW.started_by IS DISTINCT FROM kb.current_principal()) THEN
        RAISE EXCEPTION 'projection_manager_required';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        OLD.started_by IS DISTINCT FROM NEW.started_by
        OR OLD.library_id IS DISTINCT FROM NEW.library_id OR OLD.generation IS DISTINCT FROM NEW.generation
        OR OLD.indexed_revision IS DISTINCT FROM NEW.indexed_revision
        OR OLD.manifest_hash IS DISTINCT FROM NEW.manifest_hash
        OR OLD.resource_root IS DISTINCT FROM NEW.resource_root
        OR OLD.indexed_documents IS DISTINCT FROM NEW.indexed_documents
        OR OLD.content_hash IS DISTINCT FROM NEW.content_hash
    ) THEN
        RAISE EXCEPTION 'projection_inputs_are_immutable';
    END IF;
    IF NEW.resource_root <> 'viking://resources/index/p-' || NEW.profile_hash
                           || '/g-' || NEW.generation::text THEN
        RAISE EXCEPTION 'projection_namespace_mismatch';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.state <> 'building' AND (
        OLD.verified_at IS DISTINCT FROM NEW.verified_at
        OR (OLD.state = 'retired' AND NEW.state <> 'retired')
        OR NEW.state = 'building'
    ) THEN RAISE EXCEPTION 'projection_finished_is_immutable'; END IF;
    IF NEW.state = 'current' AND (TG_OP = 'INSERT' OR OLD.state <> 'current') THEN
        IF TG_OP = 'INSERT' OR OLD.state <> 'building' THEN
            RAISE EXCEPTION 'projection_activation_requires_build';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.library_id::text, 91));
        SELECT generation INTO current_revision FROM kb.library WHERE id = NEW.library_id;
        IF current_revision IS DISTINCT FROM NEW.indexed_revision
           OR NEW.verified_at IS NULL
           OR NOT EXISTS (SELECT 1 FROM kb.index_account a
                         WHERE a.library_id = NEW.library_id AND a.state = 'ready'
                           AND a.profile_hash = NEW.profile_hash) THEN
            RAISE EXCEPTION 'projection_activation_stale';
        END IF;
        SELECT count(*) INTO mapped_count FROM kb.index_document
        WHERE library_id = NEW.library_id AND generation = NEW.generation;
        SELECT count(*), encode(sha256(convert_to(coalesce(string_agg(
                   id::text || ':' || encode(sha256(convert_to(statement, 'UTF8')), 'hex'),
                   E'\n' ORDER BY id), ''), 'UTF8')), 'hex')
          INTO published_count, actual_manifest
          FROM kb.knowledge k WHERE library_id = NEW.library_id AND publication = 'published'
            AND NOT EXISTS(SELECT 1 FROM kb.knowledge_provenance p JOIN kb.source s ON s.id=p.source_id
                           WHERE p.knowledge_id=k.id AND s.publication='withdrawn');
        IF mapped_count <> NEW.indexed_documents OR published_count <> NEW.indexed_documents
           OR actual_manifest <> NEW.manifest_hash
           OR NEW.content_hash IS DISTINCT FROM encode(sha256(convert_to(NEW.profile_hash || ':'
                        || NEW.indexed_revision::text || ':' || actual_manifest, 'UTF8')), 'hex')
           OR EXISTS (
               SELECT 1 FROM kb.index_document d
               JOIN kb.knowledge k ON k.id = d.knowledge_id AND k.library_id = d.library_id
               WHERE d.library_id = NEW.library_id AND d.generation = NEW.generation
                 AND (k.publication <> 'published'
                      OR d.statement_hash <> encode(sha256(convert_to(k.statement, 'UTF8')), 'hex')
                      OR d.resource_uri <> NEW.resource_root || '/' || k.id::text || '.md')
           ) THEN
            RAISE EXCEPTION 'projection_manifest_mismatch';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER semantic_generation_guard
BEFORE INSERT OR UPDATE ON kb.index_generation
FOR EACH ROW EXECUTE FUNCTION kb.guard_semantic_generation();
REVOKE ALL ON FUNCTION kb.guard_semantic_generation() FROM PUBLIC;

-- A successful manifest is immutable. Parent cascades may remove obsolete
-- mappings, but a privileged indexer cannot silently rewrite a served generation.
CREATE FUNCTION kb.guard_semantic_document()
RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, kb AS $$
DECLARE lib uuid; gen integer;
BEGIN
    IF TG_OP = 'DELETE' THEN lib := OLD.library_id; gen := OLD.generation;
    ELSE lib := NEW.library_id; gen := NEW.generation; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(lib::text, 91));
    IF TG_OP = 'DELETE' AND (
        NOT EXISTS (SELECT 1 FROM kb.index_generation WHERE library_id=lib AND generation=gen)
        OR NOT EXISTS (SELECT 1 FROM kb.knowledge WHERE id=OLD.knowledge_id AND library_id=lib)
    ) THEN RETURN OLD; END IF;
    IF NOT EXISTS(SELECT 1 FROM kb.library l JOIN kb.membership m ON m.organisation_id=l.organisation_id
                  WHERE l.id=lib AND m.principal_id=kb.current_principal() AND m.status='active') THEN
        RAISE EXCEPTION 'projection_active_membership_required';
    END IF;
    IF TG_OP = 'UPDATE' AND (OLD.library_id IS DISTINCT FROM NEW.library_id
                            OR OLD.generation IS DISTINCT FROM NEW.generation) THEN
        RAISE EXCEPTION 'projection_document_parent_is_immutable';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM kb.index_generation
                   WHERE library_id=lib AND generation=gen AND state='building') THEN
        RAISE EXCEPTION 'projection_documents_are_immutable';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER semantic_document_guard BEFORE INSERT OR UPDATE OR DELETE
ON kb.index_document FOR EACH ROW EXECUTE FUNCTION kb.guard_semantic_document();
REVOKE ALL ON FUNCTION kb.guard_semantic_document() FROM PUBLIC;

CREATE FUNCTION kb.guard_semantic_source_scope()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, kb AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM kb.knowledge_provenance p
               JOIN kb.source s ON s.id=p.source_id
               WHERE p.knowledge_id=NEW.id AND s.library_id<>NEW.library_id) THEN
        RAISE EXCEPTION 'knowledge_source_library_mismatch';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER semantic_source_scope BEFORE UPDATE OF library_id,publication
ON kb.knowledge FOR EACH ROW EXECUTE FUNCTION kb.guard_semantic_source_scope();
REVOKE ALL ON FUNCTION kb.guard_semantic_source_scope() FROM PUBLIC;

CREATE FUNCTION kb.bump_source_semantic_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,kb AS $$
BEGIN
    IF OLD.publication IS DISTINCT FROM NEW.publication THEN
        PERFORM pg_advisory_xact_lock(hashtextextended(NEW.library_id::text,91));
        UPDATE kb.library SET generation=generation+1 WHERE id=NEW.library_id;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER source_semantic_revision AFTER UPDATE OF publication ON kb.source
FOR EACH ROW EXECUTE FUNCTION kb.bump_source_semantic_revision();
REVOKE ALL ON FUNCTION kb.bump_source_semantic_revision() FROM PUBLIC;

-- Cancellation can only retire the caller's own unfinished build. It remains
-- available after revocation, without granting access to content or accounts.
CREATE FUNCTION kb.cancel_semantic_build(p_library uuid,p_generation integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,kb AS $$
DECLARE cancelled boolean;
BEGIN
    IF NOT pg_has_role(session_user,'kb_provisioner','member') THEN
        RAISE EXCEPTION 'projection_writer_required' USING ERRCODE='insufficient_privilege';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(p_library::text,91));
    UPDATE kb.index_generation SET state='retired',finished_at=now(),updated_at=now()
    WHERE library_id=p_library AND generation=p_generation AND state='building'
      AND started_by=kb.current_principal();
    cancelled:=FOUND;
    RETURN cancelled;
END;
$$;
REVOKE ALL ON FUNCTION kb.cancel_semantic_build(uuid,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kb.cancel_semantic_build(uuid,integer) TO kb_provisioner;

COMMIT;
