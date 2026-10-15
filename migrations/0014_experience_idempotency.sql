-- Retried explicit experience writes retain the same event identity.
BEGIN;
ALTER TABLE kb.use_record ADD COLUMN idempotency_key text
    CHECK (idempotency_key IS NULL OR
        (length(btrim(idempotency_key)) > 0 AND length(idempotency_key) <= 200));
CREATE UNIQUE INDEX use_request_identity ON kb.use_record (principal_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
ALTER TABLE kb.outcome ADD COLUMN idempotency_key text
    CHECK (idempotency_key IS NULL OR
        (length(btrim(idempotency_key)) > 0 AND length(idempotency_key) <= 200));
CREATE UNIQUE INDEX outcome_request_identity ON kb.outcome (reported_by, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE OR REPLACE FUNCTION kb.freeze_use_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.rule_id<>OLD.rule_id OR NEW.rule_version_no<>OLD.rule_version_no
       OR NEW.principal_id<>OLD.principal_id
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'use identity is immutable' USING ERRCODE='check_violation';
    END IF;
    RETURN NEW;
END $$;
COMMIT;
