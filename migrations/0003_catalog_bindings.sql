-- 0003_catalog_bindings.sql — library type registry, projects, bindings, pins.
--
-- Scope: C09. Single new migration file; 0001 and 0002 are not touched.
--
-- The invariant this file exists to protect:
--
--     BINDING A LIBRARY TO A PROJECT IS NOT AN ACCESS RIGHT.
--
-- It is written here as *absence*, because absence is what a policy can
-- express: nothing in this file ever calls kb.effective_role(principal,
-- library_id) on the *target* of a link. A project link is a row in a table
-- with a policy keyed on the PROJECT's role only. If a future edit adds a
-- role lookup against the target library, tests/integration/libraries/
-- test_project_bindings.py::test_link_policies_never_consult_the_target_role
-- fails. The invariant is proved, not documented.
--
-- The second invariant, kind ⊥ audience, is likewise structural: the type
-- registry (kb.library_type) carries a kind's *metadata* and has no column
-- for an audience, and kb.library keeps audience_scope as an independent
-- column that no type definition can constrain.

BEGIN;
SET search_path = kb, public;

-- ======================================================== type registry
-- An extensible catalogue of library types, not an enum.
--
-- The five core types required by PRODUCT-SPEC are seeded below. A type
-- carries: title, description, template version, the extra fields it allows,
-- and its standard review process. Adding a type is INSERTing a row — a data
-- operation, with no DDL and no migration of already-loaded sources. That is
-- the property ARCHITECTURE.md asks for ("Тип можно расширить конфигурацией
-- без переноса уже загруженных источников"), and it is what
-- test_library_types.py::test_registering_a_type_does_not_disturb_existing_libraries
-- proves on a real database.
--
-- Known limit, stated rather than hidden: kb.library.kind is a PostgreSQL
-- ENUM created in 0001, and PostgreSQL enums are closed. A newly registered
-- type is therefore *known to the registry and the API* before it can back a
-- library; attaching one needs the enum value added by the single DDL owner.
-- Nothing already stored moves or is rewritten when that happens.
CREATE TABLE library_type (
    key                 text PRIMARY KEY
                        CHECK (key ~ '^[a-z][a-z0-9_]{1,63}$'),
    title               text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    -- genuinely unknown description stays NULL. It is never 'n/a'.
    description         text,
    template_version    integer NOT NULL DEFAULT 1 CHECK (template_version >= 1),
    -- the extra fields this type permits, as [{"name": ..., "type": ...}].
    -- A capability declaration, not user data.
    allowed_extra_fields jsonb NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(allowed_extra_fields) = 'array'),
    review_process      jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(review_process) = 'object'),
    is_core             boolean NOT NULL DEFAULT false,
    created_at          timestamptz NOT NULL DEFAULT now(),
    -- NULL = active. A retired type stays readable so that existing libraries
    -- keep resolving; retirement hides it from the default listing only.
    retired_at          timestamptz
);

COMMENT ON TABLE kb.library_type IS
    'Extensible library type registry. Contains no user content: keys, titles, '
    'templates and review processes only. Registering a type never grants '
    'access to anything.';

-- The five core types. Written out rather than derived from the enum so that
-- the registry is a real catalogue with its own metadata, and so a type can
-- exist in the registry before the enum can carry it.
INSERT INTO kb.library_type
        (key, title, description, template_version, allowed_extra_fields, review_process, is_core)
VALUES
 ('reference', 'Reference',
  'Books, articles and external material. Stored once, linked by many projects.',
  1, '[]', '{"steps":["extract","dispute_review","curator_publish"]}', true),
 ('brand',     'Brand',
  'Approved brand rules. A project pins exact versions and is blocked without them.',
  1, '[]', '{"steps":["extract","curator_publish"]}', true),
 ('project',   'Project',
  'Working material for one project. Links other libraries; the link is not a grant.',
  1, '[{"name":"client","type":"text"},{"name":"deadline","type":"date"}]',
  '{"steps":["extract","dispute_review","curator_publish"]}', true),
 ('playbook',  'Playbook',
  'Repeatable procedure assembled from rules of other libraries.',
  1, '[]', '{"steps":["extract","dispute_review","curator_publish"]}', true),
 ('experience','Experience',
  'Outcomes of applying exact rule versions. Never inherits an audience from a rule.',
  1, '[{"name":"where","type":"text"}]', '{"steps":["extract","curator_publish"]}', true);

-- Narrow, SECURITY DEFINER lookup used by the registration trigger below.
-- SECURITY DEFINER because the trigger runs inside a library INSERT whose
-- caller may hold no grant on the registry; the fixed search_path is the
-- documented guard against a hijacked one. It returns a boolean and nothing
-- else, so it can never be used to read registry content through a policy.
CREATE OR REPLACE FUNCTION kb.library_type_is_registered(p_key text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = kb, public
AS $$
    SELECT EXISTS (
        SELECT 1 FROM kb.library_type t
        WHERE t.key = p_key AND t.retired_at IS NULL
    );
$$;

-- A library cannot exist whose kind is not in the registry. Without this, the
-- registry would be decorative and could drift from the enum.
CREATE OR REPLACE FUNCTION kb.reject_unregistered_library_type()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = kb, public
AS $$
BEGIN
    IF NOT kb.library_type_is_registered(NEW.kind::text) THEN
        RAISE EXCEPTION
            'library type % is not registered; register it before creating a library of that kind',
            NEW.kind
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER library_type_must_be_registered
    BEFORE INSERT OR UPDATE OF kind ON kb.library
    FOR EACH ROW EXECUTE FUNCTION kb.reject_unregistered_library_type();

-- ============================================================== projects
-- A project IS a library of kind 'project' — there is no second application
-- and no second database (ARCHITECTURE.md §4). This table holds only the
-- state a library does not have: a description and when the manifest was
-- last pinned. id == library id, so kb.effective_role() applies unchanged.
CREATE TABLE project (
    id          uuid PRIMARY KEY REFERENCES kb.library(id) ON DELETE CASCADE,
    -- NULL when the project has no description. Never a placeholder string.
    description text,
    pinned_at   timestamptz NOT NULL DEFAULT now()
);

-- A project is a library of kind 'project', and the row is worthless if it is
-- not. A CHECK constraint cannot contain a subquery in PostgreSQL, so the
-- invariant is a trigger. It closes the door that would otherwise let a
-- project row hang off a brand or reference library.
--
-- The fixed search_path is not decoration. plpgsql resolves a DECLARE type when
-- the function is first *called*, using the caller's search_path — and the
-- runtime role's search_path is `public`, so an unqualified `library_kind`
-- fails at runtime with "type does not exist" even though CREATE FUNCTION
-- succeeded. Every function below is pinned for the same reason, and the pin
-- also keeps a hijacked search_path from redefining what they mean.
CREATE OR REPLACE FUNCTION kb.reject_non_project_library()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = kb, public
AS $$
DECLARE
    actual_kind kb.library_kind;
BEGIN
    SELECT l.kind INTO actual_kind FROM kb.library l WHERE l.id = NEW.id;
    IF actual_kind IS DISTINCT FROM 'project'::kb.library_kind THEN
        RAISE EXCEPTION
            'library % has kind %, but a project row requires kind ''project''',
            NEW.id, coalesce(actual_kind::text, '<missing>')
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER project_requires_project_kind
    BEFORE INSERT OR UPDATE ON kb.project
    FOR EACH ROW EXECUTE FUNCTION kb.reject_non_project_library();

-- ------------------------------------------------- project -> library link
-- "Проект связывает разрешённые библиотеки". Linking is a statement about the
-- project's requirements, not a distribution of rights.
CREATE TABLE project_library_link (
    project_id  uuid NOT NULL REFERENCES kb.project(id) ON DELETE CASCADE,
    library_id  uuid NOT NULL REFERENCES kb.library(id) ON DELETE CASCADE,
    -- A required link is a dependency: missing rights block "full context"
    -- rather than silently shrinking it (ARCHITECTURE.md §4, A12).
    is_required boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, library_id),
    -- A project linking itself would make the required-context check a no-op.
    CONSTRAINT project_cannot_link_itself CHECK (project_id <> library_id)
);

COMMENT ON TABLE kb.project_library_link IS
    'Requirement edges from a project to the libraries it needs. NOT an ACL. '
    'There is no column here that names a principal and no policy here that '
    'reads the role on library_id: membership of project_id confers nothing on '
    'library_id.';

-- A project curator must not be able to learn which library ids are real.
--
-- The foreign key above is a referential-integrity trigger, and it runs as the
-- constraint owner, so it does not see the caller's RLS. Without this guard a
-- curator of any project could POST an arbitrary uuid and read the answer off
-- the difference between "exists" and "does not exist" — an existence oracle
-- over every library in the installation. ACCESS-MODEL §9 names exactly this
-- shape as A20.
--
-- The answer given is the same one the caller would get for a library that
-- genuinely does not exist, so the two are indistinguishable.
--
-- A session with no transport principal is the migration or owner session. It
-- bypasses RLS by role, it is not the audience of this threat, and the CHECK
-- constraints behind it still apply — so the guard steps aside for it.
CREATE OR REPLACE FUNCTION kb.reject_link_to_invisible_library()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = kb, public
AS $$
DECLARE
    caller uuid := kb.current_principal();
BEGIN
    IF caller IS NULL THEN
        RETURN NEW;
    END IF;
    IF kb.effective_role(caller, NEW.library_id) IS NULL THEN
        RAISE EXCEPTION 'no such library'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER project_link_needs_a_visible_target
    BEFORE INSERT OR UPDATE OF library_id ON kb.project_library_link
    FOR EACH ROW EXECUTE FUNCTION kb.reject_link_to_invisible_library();

-- ------------------------------------------------------ project rule pins
-- "Проект ... закрепляет обязательные версии правил" (PRODUCT-SPEC).
--
-- Two database-enforced rules, and both are load-bearing:
--
--   PRIMARY KEY (project_id, rule_id)
--       a project mandates exactly one version of a given rule. Moving to a
--       corrected version is a deliberate update, not a second mandate sitting
--       beside the first. Two contradictory pins would make "the project's
--       requirement" unanswerable.
--
--   FOREIGN KEY (rule_id, version_no) -> kb.rule(id, version_no)
--       a pin must name a version the rule actually has, so "whatever the
--       current version is" is not expressible. kb.rule's primary key is (id)
--       alone, so (id, version_no) needs an explicit unique constraint to be
--       referenceable; adding it here costs nothing and is what makes the pin
--       mean something. An outcome recorded against "the rule" is
--       unattributable after a correction — the same reason the contract pins
--       an experience to a version.
ALTER TABLE kb.rule
    ADD CONSTRAINT rule_id_version_pinnable UNIQUE (id, version_no);

CREATE TABLE project_rule_pin (
    project_id  uuid NOT NULL REFERENCES kb.project(id) ON DELETE CASCADE,
    rule_id     uuid NOT NULL,
    version_no  integer NOT NULL CHECK (version_no >= 1),
    is_required boolean NOT NULL DEFAULT true,
    -- Ordering for context assembly: approved brand constraints outrank
    -- project-specific rules, which outrank general recommendations
    -- (ARCHITECTURE.md §9). A number, not an enum, so a project can insert
    -- its own band without a vocabulary change.
    priority    integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, rule_id),
    -- An outcome recorded against "the rule" is unattributable after a
    -- correction. The same reasoning is why a pin is a version, not a rule.
    CONSTRAINT pin_names_one_exact_version
        FOREIGN KEY (rule_id, version_no) REFERENCES kb.rule(id, version_no)
        ON DELETE CASCADE
);

-- ============================================== library creation bootstrap
-- 0002's grant policy is `USING/WITH CHECK (role_rank(effective_role(...)) >= 40)`.
-- A library that does not exist yet therefore has no effective role for
-- anybody, so the creator cannot INSERT their own manager grant: RLS rejects
-- the bootstrap with no way to reach it from the runtime role. That is the
-- right default and still needs one honest way out.
--
-- This is that way, and it is deliberately narrow:
--   * SECURITY DEFINER, so it can write the first grant. It is the only such
--     function in this file, and it inserts exactly two rows.
--   * the principal is forced to current_principal(). You can create a library
--     for yourself; you cannot create one and appoint yourself manager on
--     somebody else's behalf, and there is no role parameter at all.
--   * no identity means no call. A request without a verified principal is
--     refused before a row exists, rather than creating a library nobody owns.
--   * fixed search_path, per ACCESS-MODEL §4.
-- The create right itself is unchanged: 0002 already lets any authenticated
-- principal INSERT a library.
CREATE OR REPLACE FUNCTION kb.create_owned_library(
    p_organisation uuid,
    p_name         text,
    p_kind         kb.library_kind,
    p_audience     text,
    p_principal    uuid
) RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = kb, public
AS $$
DECLARE
    new_id uuid := gen_random_uuid();
BEGIN
    IF kb.current_principal() IS NULL THEN
        RAISE EXCEPTION 'no authenticated principal'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF p_principal IS DISTINCT FROM kb.current_principal() THEN
        RAISE EXCEPTION
            'a library may only be created for the authenticated principal; '
            'appointing a manager for somebody else is a separate, audited grant'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    INSERT INTO kb.library (id, organisation_id, name, kind, audience_scope)
    VALUES (new_id, p_organisation, p_name, p_kind, p_audience);
    INSERT INTO kb.library_grant (library_id, principal_id, role)
    VALUES (new_id, p_principal, 'manager');
    RETURN new_id;
END;
$$;

-- ================================================================ RLS
ALTER TABLE kb.library_type        ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.library_type        FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.project             ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.project             FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.project_library_link ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.project_library_link FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.project_rule_pin    ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.project_rule_pin    FORCE  ROW LEVEL SECURITY;

-- The registry is a vocabulary, not user data: no library names, no counts,
-- no content. Reading it tells a caller nothing about anybody's libraries.
CREATE POLICY library_type_read ON kb.library_type FOR SELECT
    USING (kb.current_principal() IS NOT NULL);

-- Registration is a configuration write, so it is open to any authenticated
-- principal. It cannot create a library, a grant or a row of content: kb_app
-- holds no UPDATE/DELETE on this table and no enum value for a non-core key.
-- Editing or retiring a type is a migration, not a runtime action, and is
-- deliberately left without a policy (default deny).
CREATE POLICY library_type_register ON kb.library_type FOR INSERT
    WITH CHECK (kb.current_principal() IS NOT NULL AND is_core = false);

-- project: the id is a library id, so the effective role is the library role.
CREATE POLICY project_read ON kb.project FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), id)) >= 10);

CREATE POLICY project_write ON kb.project FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), id)) >= 30)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), id)) >= 30);

-- project_library_link: keyed on the PROJECT's role and on nothing else.
--
-- Read the USING clause. effective_role is called with project_id, once.
-- library_id appears in no role expression. That omission is the whole point
-- of the card, and the structural test asserts on it.
--
-- A member therefore learns that their project depends on something they may
-- not open. That is the honest answer (A12: the required context is not
-- complete). It is a project-member fact, not a description of the closed
-- library: the API never returns library_id, name or kind for a link the
-- caller cannot resolve, so the link is not a directory of invisible objects.
CREATE POLICY project_link_read ON kb.project_library_link FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), project_id)) >= 10);

CREATE POLICY project_link_write ON kb.project_library_link FOR ALL
    USING      (kb.role_rank(kb.effective_role(kb.current_principal(), project_id)) >= 30)
    WITH CHECK (kb.role_rank(kb.effective_role(kb.current_principal(), project_id)) >= 30);

-- project_rule_pin: readable by anyone with reader on the PROJECT, and by
-- nobody else.
--
-- Keyed on the project alone, on purpose, for the same reason the link table
-- is. If the policy also demanded read access to the rule, a mandatory pin
-- whose rule sits in a closed library would vanish from the manifest, the
-- count would shrink to match, and the project would report a *complete*
-- context while quietly withholding a brand rule. An incomplete context that
-- admits it is incomplete is the correct answer; a silently truncated one is
-- exactly the failure A12 forbids.
--
-- The residual exposure is one opaque rule UUID per pin, to a principal who
-- can already read the project manifest. The rule's text, title, kind and
-- library stay behind kb.rule's own RLS, and the API never returns the id of
-- a pin it could not resolve. A UUID is not a name and resolves to nothing.
CREATE POLICY project_pin_read ON kb.project_rule_pin FOR SELECT
    USING (kb.role_rank(kb.effective_role(kb.current_principal(), project_id)) >= 10);

-- Pinning requires curator on the rule's library as well as on the project:
-- you cannot make a mandatory rule out of one you may not read.
CREATE POLICY project_pin_write ON kb.project_rule_pin FOR ALL
    USING (
        kb.role_rank(kb.effective_role(kb.current_principal(), project_id)) >= 30
        AND EXISTS (
            SELECT 1 FROM kb.rule r
            WHERE r.id = rule_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), r.library_id)) >= 30
        )
    )
    WITH CHECK (
        kb.role_rank(kb.effective_role(kb.current_principal(), project_id)) >= 30
        AND EXISTS (
            SELECT 1 FROM kb.rule r
            WHERE r.id = rule_id
              AND kb.role_rank(kb.effective_role(kb.current_principal(), r.library_id)) >= 30
        )
    );

-- =============================================================== grants
-- 0002 granted ON ALL TABLES IN SCHEMA kb, which is a one-time grant over the
-- tables that existed at that moment. Everything above was created after it,
-- so it is granted here explicitly, and no wider.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    kb.project, kb.project_library_link, kb.project_rule_pin TO kb_app;

-- The registry is append-only from the runtime's point of view: register a
-- type, never rewrite or delete one. kb_app therefore receives no UPDATE and
-- no DELETE here, on top of there being no policy for either.
GRANT SELECT, INSERT ON kb.library_type TO kb_app;

GRANT EXECUTE ON FUNCTION kb.library_type_is_registered(text) TO kb_app;
GRANT EXECUTE ON FUNCTION kb.library_type_is_registered(text) TO kb_worker;
GRANT EXECUTE ON FUNCTION kb.create_owned_library(uuid, text, kb.library_kind, text, uuid)
    TO kb_app;
-- deliberately NOT granted to kb_worker: the worker processes libraries, it
-- does not create them.

-- kb_worker is deliberately left without project and link access. Processing a
-- library's own content does not require knowing which projects depend on it,
-- and the narrower the worker, the smaller the blast radius of a stolen job
-- credential. C09 does not widen it.

COMMIT;
