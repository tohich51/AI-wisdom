-- 0031_library_type_binding.sql — bind custom registry types to core library kinds.
-- Existing libraries remain NULL; the closed kb.library_kind enum is unchanged.

BEGIN;
SET search_path = kb, public;

ALTER TABLE kb.library_type
    ADD COLUMN base_kind kb.library_kind NOT NULL DEFAULT 'reference';

UPDATE kb.library_type
   SET base_kind = key::kb.library_kind
 WHERE is_core;

ALTER TABLE kb.library
    ADD COLUMN type_key text NULL REFERENCES kb.library_type(key);

-- Keep the core-kind registry authority and validate a non-NULL template
-- binding only when it is new or when kind/type_key actually changes. This
-- leaves libraries bound to a retired type readable and renameable.
CREATE OR REPLACE FUNCTION kb.reject_unregistered_library_type()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = kb, public
AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.kind IS DISTINCT FROM OLD.kind THEN
        IF NOT kb.library_type_is_registered(NEW.kind::text)
           OR NOT EXISTS (
               SELECT 1
                 FROM kb.library_type t
                WHERE t.key = NEW.kind::text
                  AND t.is_core
           ) THEN
            RAISE EXCEPTION
                'library type % is not registered as an active core kind; register it before creating a library of that kind',
                NEW.kind
                USING ERRCODE = 'foreign_key_violation';
        END IF;
    END IF;

    IF NEW.type_key IS NOT NULL
       AND (
           TG_OP = 'INSERT'
           OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.type_key IS DISTINCT FROM OLD.type_key
       ) THEN
        IF NOT EXISTS (
            SELECT 1
              FROM kb.library_type t
             WHERE t.key = NEW.type_key
               AND t.retired_at IS NULL
               AND t.base_kind = NEW.kind
        ) THEN
            RAISE EXCEPTION
                'library type key % is unknown, retired, or incompatible with library kind %',
                NEW.type_key, NEW.kind
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER library_type_must_be_registered ON kb.library;

CREATE TRIGGER library_type_must_be_registered
    BEFORE INSERT OR UPDATE OF kind, type_key ON kb.library
    FOR EACH ROW EXECUTE FUNCTION kb.reject_unregistered_library_type();

-- The old five-argument function remains the sole authentication and bootstrap
-- grant path. This overload calls it first, then binds only the generated id.
-- Any binding-trigger failure aborts the function call and rolls all of it back.
CREATE FUNCTION kb.create_owned_library(
    p_organisation uuid,
    p_name         text,
    p_kind         kb.library_kind,
    p_audience     text,
    p_principal    uuid,
    p_type_key     text
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    created_id uuid;
BEGIN
    created_id := kb.create_owned_library(
        p_organisation,
        p_name,
        p_kind,
        p_audience,
        p_principal
    );

    IF p_type_key IS NOT NULL THEN
        UPDATE kb.library
           SET type_key = p_type_key
         WHERE id = created_id;
    END IF;

    RETURN created_id;
END;
$$;

REVOKE EXECUTE ON FUNCTION kb.create_owned_library(
    uuid, text, kb.library_kind, text, uuid, text
) FROM PUBLIC;

-- 0003 grants library creation only to the application runtime role.
GRANT EXECUTE ON FUNCTION kb.create_owned_library(
    uuid, text, kb.library_kind, text, uuid, text
) TO kb_app;

COMMIT;
