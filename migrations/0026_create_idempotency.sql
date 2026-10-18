-- Principal-owned receipts for create-library, create-project and create-rule retries.
-- Only the request digest and resulting target id are retained.
BEGIN;

CREATE TABLE kb.operation_receipt (
    principal_id uuid NOT NULL,
    operation_type text NOT NULL
        CHECK (operation_type IN ('library_create', 'project_create', 'rule_create')),
    idempotency_key text NOT NULL
        CHECK (length(btrim(idempotency_key)) > 0 AND length(idempotency_key) <= 200),
    request_hash text NOT NULL
        CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    target_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT operation_receipt_identity
        PRIMARY KEY (principal_id, operation_type, idempotency_key)
);

ALTER TABLE kb.operation_receipt ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.operation_receipt FORCE ROW LEVEL SECURITY;

CREATE POLICY operation_receipt_select_own
    ON kb.operation_receipt
    FOR SELECT
    TO kb_app
    USING (principal_id = kb.current_principal());

CREATE POLICY operation_receipt_insert_own
    ON kb.operation_receipt
    FOR INSERT
    TO kb_app
    WITH CHECK (principal_id = kb.current_principal());

REVOKE ALL ON TABLE kb.operation_receipt FROM PUBLIC;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE kb.operation_receipt FROM kb_app;
GRANT SELECT, INSERT ON TABLE kb.operation_receipt TO kb_app;

CREATE FUNCTION kb.reject_operation_receipt_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'operation receipts are immutable'
        USING ERRCODE = '55000';
END
$$;

REVOKE ALL ON FUNCTION kb.reject_operation_receipt_mutation() FROM PUBLIC;

CREATE TRIGGER operation_receipt_immutable_rows
    BEFORE UPDATE OR DELETE ON kb.operation_receipt
    FOR EACH ROW
    EXECUTE FUNCTION kb.reject_operation_receipt_mutation();

CREATE TRIGGER operation_receipt_immutable_truncate
    BEFORE TRUNCATE ON kb.operation_receipt
    FOR EACH STATEMENT
    EXECUTE FUNCTION kb.reject_operation_receipt_mutation();

COMMIT;
