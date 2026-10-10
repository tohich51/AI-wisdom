-- Runtime integration safeguards. No content is inserted.
BEGIN;
SET search_path = kb, public;

ALTER TABLE kb.rule DROP CONSTRAINT rule_library_id_version_no_key;
ALTER TABLE kb.rule ADD COLUMN lineage_id uuid;
UPDATE kb.rule SET lineage_id = id;
ALTER TABLE kb.rule ADD CONSTRAINT rule_lineage_fk
    FOREIGN KEY (lineage_id) REFERENCES kb.rule(id) ON DELETE RESTRICT;
ALTER TABLE kb.rule ADD CONSTRAINT rule_lineage_version UNIQUE (lineage_id, version_no);
ALTER TABLE kb.rule ADD CONSTRAINT rule_exact_version UNIQUE (id, version_no);
ALTER TABLE kb.rule ADD COLUMN inputs jsonb NOT NULL DEFAULT '[]';
ALTER TABLE kb.rule ADD COLUMN preconditions jsonb NOT NULL DEFAULT '[]';
ALTER TABLE kb.rule ADD COLUMN exceptions jsonb NOT NULL DEFAULT '[]';
ALTER TABLE kb.rule ADD CONSTRAINT rule_lists_are_arrays CHECK (
    jsonb_typeof(inputs) = 'array' AND jsonb_typeof(preconditions) = 'array'
    AND jsonb_typeof(exceptions) = 'array'
);
CREATE FUNCTION kb.bind_rule_lineage() RETURNS trigger
LANGUAGE plpgsql SET search_path = kb, public AS $$
DECLARE parent kb.rule%ROWTYPE;
BEGIN
    IF NEW.lineage_id IS NULL THEN NEW.lineage_id := NEW.id; END IF;
    IF NEW.lineage_id <> NEW.id THEN
        SELECT * INTO parent FROM kb.rule WHERE id = NEW.lineage_id FOR KEY SHARE;
        IF NOT FOUND OR parent.library_id <> NEW.library_id
           OR parent.lineage_id <> parent.id THEN
            RAISE EXCEPTION 'forbidden' USING ERRCODE = 'insufficient_privilege';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER rule_lineage_bound BEFORE INSERT ON kb.rule
    FOR EACH ROW EXECUTE FUNCTION kb.bind_rule_lineage();
ALTER TABLE kb.rule ADD CONSTRAINT rule_lineage_present CHECK (lineage_id IS NOT NULL);

ALTER TABLE kb.use_record ADD CONSTRAINT use_exact_rule
    FOREIGN KEY (rule_id, rule_version_no) REFERENCES kb.rule(id, version_no);
ALTER TABLE kb.use_record ADD CONSTRAINT use_exact_identity
    UNIQUE (id, rule_id, rule_version_no);
ALTER TABLE kb.experience ADD CONSTRAINT experience_exact_use
    FOREIGN KEY (use_id, rule_id, rule_version_no)
    REFERENCES kb.use_record(id, rule_id, rule_version_no);

CREATE OR REPLACE FUNCTION kb.reject_published_rule_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = kb, public AS $$
BEGIN
    IF OLD.publication = 'published' THEN
        RAISE EXCEPTION 'published rule is immutable' USING ERRCODE = 'check_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    IF NEW.id <> OLD.id OR NEW.library_id <> OLD.library_id
       OR NEW.version_no <> OLD.version_no OR NEW.lineage_id <> OLD.lineage_id THEN
        RAISE EXCEPTION 'rule version identity is immutable' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER rule_published_immutable ON kb.rule;
CREATE TRIGGER rule_published_immutable BEFORE UPDATE OR DELETE ON kb.rule
    FOR EACH ROW EXECUTE FUNCTION kb.reject_published_rule_update();

CREATE FUNCTION kb.check_rule_publication() RETURNS trigger
LANGUAGE plpgsql SET search_path = kb, public AS $$
BEGIN
    IF NEW.publication = 'published' THEN
        IF kb.current_principal() IS NOT NULL AND (
            COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(), NEW.library_id)),0)<30
            OR pg_has_role(current_user, 'kb_worker', 'member')
        ) THEN
            RAISE EXCEPTION 'forbidden' USING ERRCODE = 'insufficient_privilege';
        END IF;
        IF COALESCE(btrim(NEW.when_to_apply),'') = ''
           OR COALESCE(btrim(NEW.expected_effect),'') = ''
           OR NOT EXISTS (
               SELECT 1 FROM kb.rule_action WHERE rule_id=NEW.id AND btrim(body)<>''
           ) THEN
            RAISE EXCEPTION 'incomplete rule' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER rule_publication_checked BEFORE INSERT OR UPDATE ON kb.rule
    FOR EACH ROW EXECUTE FUNCTION kb.check_rule_publication();

ALTER TABLE kb.rule_action ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.rule_action FORCE ROW LEVEL SECURITY;
CREATE POLICY rule_action_read ON kb.rule_action FOR SELECT USING (
    EXISTS (SELECT 1 FROM kb.rule r WHERE r.id=rule_id
        AND kb.role_rank(kb.effective_role(kb.current_principal(),r.library_id))>=10)
);
CREATE POLICY rule_action_write ON kb.rule_action FOR ALL USING (
    EXISTS (SELECT 1 FROM kb.rule r WHERE r.id=rule_id
        AND kb.role_rank(kb.effective_role(kb.current_principal(),r.library_id))>=20)
) WITH CHECK (
    EXISTS (SELECT 1 FROM kb.rule r WHERE r.id=rule_id
        AND kb.role_rank(kb.effective_role(kb.current_principal(),r.library_id))>=20)
);
CREATE FUNCTION kb.check_action_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path = kb, public AS $$
DECLARE parent kb.rule%ROWTYPE; target uuid;
BEGIN
    IF TG_OP='DELETE' THEN target:=OLD.rule_id; ELSE target:=NEW.rule_id; END IF;
    IF TG_OP='UPDATE' AND NEW.rule_id<>OLD.rule_id THEN
        RAISE EXCEPTION 'action identity is immutable' USING ERRCODE='check_violation';
    END IF;
    SELECT * INTO parent FROM kb.rule WHERE id=target FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'forbidden' USING ERRCODE='insufficient_privilege';
    END IF;
    IF parent.publication='published' THEN
        RAISE EXCEPTION 'published actions are immutable' USING ERRCODE='check_violation';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER action_mutation_checked BEFORE INSERT OR UPDATE OR DELETE ON kb.rule_action
    FOR EACH ROW EXECUTE FUNCTION kb.check_action_mutation();

CREATE FUNCTION kb.check_knowledge_publication() RETURNS trigger
LANGUAGE plpgsql SET search_path = kb, public AS $$
BEGIN
    IF TG_OP='UPDATE' AND OLD.publication='published' THEN
        RAISE EXCEPTION 'published knowledge is immutable' USING ERRCODE='check_violation';
    END IF;
    IF NEW.publication='published' AND kb.current_principal() IS NOT NULL AND (
        COALESCE(kb.role_rank(kb.effective_role(kb.current_principal(),NEW.library_id)),0)<30
        OR pg_has_role(current_user,'kb_worker','member')
    ) THEN
        RAISE EXCEPTION 'forbidden' USING ERRCODE='insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER knowledge_publication_checked BEFORE INSERT OR UPDATE ON kb.knowledge
    FOR EACH ROW EXECUTE FUNCTION kb.check_knowledge_publication();

CREATE FUNCTION kb.check_use_version() RETURNS trigger
LANGUAGE plpgsql SET search_path = kb, public AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM kb.rule r WHERE r.id=NEW.rule_id
        AND r.version_no=NEW.rule_version_no AND r.publication='published'
        AND (kb.current_principal() IS NULL OR
            (NEW.principal_id=kb.current_principal() AND
             kb.role_rank(kb.effective_role(kb.current_principal(),r.library_id))>=10))
    ) THEN
        RAISE EXCEPTION 'forbidden' USING ERRCODE='insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER use_version_checked BEFORE INSERT ON kb.use_record
    FOR EACH ROW EXECUTE FUNCTION kb.check_use_version();
CREATE FUNCTION kb.freeze_use_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rule_id<>OLD.rule_id OR NEW.rule_version_no<>OLD.rule_version_no
       OR NEW.principal_id<>OLD.principal_id THEN
        RAISE EXCEPTION 'use identity is immutable' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER use_identity_frozen BEFORE UPDATE ON kb.use_record
    FOR EACH ROW EXECUTE FUNCTION kb.freeze_use_identity();
CREATE FUNCTION kb.append_only_outcome() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append a correction event' USING ERRCODE='check_violation';
END $$;
CREATE TRIGGER outcome_append_only BEFORE UPDATE OR DELETE ON kb.outcome
    FOR EACH ROW EXECUTE FUNCTION kb.append_only_outcome();

CREATE TABLE kb.rule_provenance (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id uuid NOT NULL REFERENCES kb.rule(id) ON DELETE CASCADE,
    source_id uuid NOT NULL REFERENCES kb.source(id) ON DELETE CASCADE,
    fragment_id uuid REFERENCES kb.fragment(id),
    UNIQUE NULLS NOT DISTINCT (rule_id, source_id, fragment_id)
);
ALTER TABLE kb.rule_provenance ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.rule_provenance FORCE ROW LEVEL SECURITY;
CREATE POLICY rule_provenance_read ON kb.rule_provenance FOR SELECT USING (
    EXISTS (SELECT 1 FROM kb.rule r WHERE r.id=rule_id
        AND kb.role_rank(kb.effective_role(kb.current_principal(),r.library_id))>=10)
);
CREATE POLICY rule_provenance_write ON kb.rule_provenance FOR ALL USING (
    EXISTS (SELECT 1 FROM kb.rule r WHERE r.id=rule_id
        AND r.publication<>'published'
        AND kb.role_rank(kb.effective_role(kb.current_principal(),r.library_id))>=20)
) WITH CHECK (
    EXISTS (SELECT 1 FROM kb.rule r JOIN kb.source s ON s.library_id=r.library_id
        WHERE r.id=rule_id AND s.id=source_id AND r.publication<>'published'
        AND kb.role_rank(kb.effective_role(kb.current_principal(),r.library_id))>=20)
    AND (fragment_id IS NULL OR EXISTS (
        SELECT 1 FROM kb.fragment f WHERE f.id=fragment_id
            AND f.source_id=rule_provenance.source_id
    ))
);
CREATE TRIGGER rule_provenance_mutation_checked
    BEFORE INSERT OR UPDATE OR DELETE ON kb.rule_provenance
    FOR EACH ROW EXECUTE FUNCTION kb.check_action_mutation();
CREATE POLICY fragment_write ON kb.fragment FOR ALL USING (
    EXISTS (SELECT 1 FROM kb.source s WHERE s.id=source_id
        AND kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id))>=20)
) WITH CHECK (
    EXISTS (SELECT 1 FROM kb.source s WHERE s.id=source_id
        AND kb.role_rank(kb.effective_role(kb.current_principal(),s.library_id))>=20)
);
CREATE POLICY knowledge_provenance_write ON kb.knowledge_provenance FOR ALL USING (
    EXISTS (SELECT 1 FROM kb.knowledge k WHERE k.id=knowledge_id
        AND k.publication<>'published'
        AND kb.role_rank(kb.effective_role(kb.current_principal(),k.library_id))>=20)
) WITH CHECK (
    EXISTS (SELECT 1 FROM kb.knowledge k JOIN kb.source s ON s.library_id=k.library_id
        WHERE k.id=knowledge_id AND s.id=source_id AND k.publication<>'published'
        AND kb.role_rank(kb.effective_role(kb.current_principal(),k.library_id))>=20)
    AND (fragment_id IS NULL OR EXISTS (
        SELECT 1 FROM kb.fragment f WHERE f.id=fragment_id
            AND f.source_id=knowledge_provenance.source_id
    ))
);

CREATE FUNCTION kb.check_knowledge_provenance_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path=kb,public AS $$
DECLARE parent kb.knowledge%ROWTYPE; target uuid;
BEGIN
    IF TG_OP='DELETE' THEN target:=OLD.knowledge_id; ELSE target:=NEW.knowledge_id; END IF;
    IF TG_OP='UPDATE' AND NEW.knowledge_id<>OLD.knowledge_id THEN
        RAISE EXCEPTION 'provenance identity is immutable' USING ERRCODE='check_violation';
    END IF;
    SELECT * INTO parent FROM kb.knowledge WHERE id=target FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'forbidden' USING ERRCODE='insufficient_privilege'; END IF;
    IF parent.publication='published' THEN
        RAISE EXCEPTION 'published provenance is immutable' USING ERRCODE='check_violation';
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER knowledge_provenance_frozen BEFORE INSERT OR UPDATE OR DELETE
    ON kb.knowledge_provenance FOR EACH ROW
    EXECUTE FUNCTION kb.check_knowledge_provenance_mutation();
CREATE FUNCTION kb.immutable_fragment() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'canonical fragments are immutable' USING ERRCODE='check_violation';
END $$;
CREATE TRIGGER fragment_frozen BEFORE UPDATE ON kb.fragment
    FOR EACH ROW EXECUTE FUNCTION kb.immutable_fragment();
ALTER TABLE kb.job ADD COLUMN source_id uuid REFERENCES kb.source(id);
ALTER TABLE kb.source ADD CONSTRAINT source_exact_library UNIQUE(id,library_id);
ALTER TABLE kb.job ADD CONSTRAINT job_source_library
    FOREIGN KEY(source_id,library_id) REFERENCES kb.source(id,library_id);
ALTER TABLE kb.job ADD COLUMN submitted_by uuid;
ALTER TABLE kb.job ADD COLUMN error_code text;
ALTER TABLE kb.job ADD COLUMN started_at timestamptz;
ALTER TABLE kb.job ADD COLUMN completed_at timestamptz;
ALTER TABLE kb.job ADD COLUMN queue_id bigint;
GRANT SELECT,INSERT,UPDATE,DELETE ON kb.rule_provenance TO kb_app;
GRANT SELECT ON kb.source_version, kb.fragment, kb.knowledge, kb.rule,
    kb.rule_action, kb.knowledge_provenance, kb.rule_provenance TO kb_worker;
GRANT INSERT ON kb.fragment, kb.knowledge, kb.rule, kb.rule_action,
    kb.knowledge_provenance, kb.rule_provenance TO kb_worker;
GRANT UPDATE ON kb.source, kb.job, kb.rule, kb.knowledge TO kb_worker;
COMMIT;
