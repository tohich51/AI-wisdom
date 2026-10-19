BEGIN;

ALTER TABLE kb.operation_receipt
    DROP CONSTRAINT operation_receipt_operation_type_check;

ALTER TABLE kb.operation_receipt
    ADD CONSTRAINT operation_receipt_operation_type_check
    CHECK (
        operation_type IN (
            'library_create',
            'project_create',
            'rule_create',
            'semantic_concept_create',
            'semantic_relation_create'
        )
    );

COMMIT;
