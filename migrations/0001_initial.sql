-- 0001_initial.sql — Knowledge Hub core schema.
--
-- Ownership: this file is the single DDL owner. Do not add a second migration
-- path for the same objects.
--
-- Bootstrap guarantee: applying this migration creates STRUCTURE ONLY. No
-- user content, no sample source, no sample rule. An empty install is empty.

BEGIN;

-- ---------------------------------------------------------------- roles
-- Four distinct roles, deliberately not one:
--   kb_schema_owner  owns the objects (migrations only)
--   kb_app           the gateway. RLS applies to it. no BYPASSRLS, no SUPERUSER
--   kb_worker        the queue worker. narrower grants than kb_app
--   (Keycloak lives in its own database and never appears here)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_schema_owner') THEN
        CREATE ROLE kb_schema_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_app') THEN
        -- deliberately: NOSUPERUSER NOBYPASSRLS is the default, stated here so
        -- a future ALTER ROLE cannot quietly widen it unnoticed.
        CREATE ROLE kb_app LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kb_worker') THEN
        CREATE ROLE kb_worker LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS kb AUTHORIZATION kb_schema_owner;
SET search_path = kb, public;

-- ------------------------------------------------------------- extensions
-- No pgcrypto requirement. gen_random_uuid() has been in core since
-- PostgreSQL 13, and the target is 16. Verified during C05: the pip-packaged
-- PostgreSQL used for native runs ships only plpgsql and vector, so requiring
-- pgcrypto would make the schema unapplyable there for no gain. On
-- PostgreSQL 12 or older, install pgcrypto separately.

-- --------------------------------------------------------- enum vocabulary
-- Mirrors kb.contracts.enums. Closed types: an unknown status is an error
-- rather than a free string that drifts between the API and the database.
CREATE TYPE library_kind    AS ENUM ('reference','brand','project','playbook','experience');
CREATE TYPE library_role    AS ENUM ('reader','contributor','curator','manager');
CREATE TYPE processing_status AS ENUM ('queued','running','partial','needs_ocr','failed','done');
CREATE TYPE publication_status AS ENUM ('draft','needs_review','published','withdrawn');
CREATE TYPE locator_kind    AS ENUM ('pdf_file_page','pdf_printed_label','epub_chapter',
                                     'epub_spine','epub_paragraph','docx_paragraph',
                                     'docx_table','docx_cell','html_snapshot','none');
CREATE TYPE verification_status AS ENUM ('unverified','cited','disputed','refuted');
CREATE TYPE usage_state     AS ENUM ('retrieved','applied','outcomed','corrected');

-- ----------------------------------------------------------------- org
CREATE TABLE organisation (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------ libraries
-- kind and audience are independent axes: a brand library may be private to a
-- project, a reference library may be readable by everyone invited.
CREATE TABLE library (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organisation_id uuid NOT NULL REFERENCES organisation(id) ON DELETE CASCADE,
    name            text NOT NULL,
    kind            library_kind NOT NULL,
    audience_scope  text NOT NULL DEFAULT 'private'
                    CHECK (audience_scope IN ('private','invited','all_invited')),
    generation      integer NOT NULL DEFAULT 1 CHECK (generation >= 1),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (organisation_id, name)
);

-- --------------------------------------------------------------- grants
-- Access is per library. Membership in a project library does NOT confer
-- access to the libraries that project links.
CREATE TABLE library_grant (
    library_id   uuid NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    principal_id uuid NOT NULL,
    role         library_role NOT NULL,
    granted_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (library_id, principal_id)
);

-- -------------------------------------------------------------- sources
CREATE TABLE source (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id    uuid NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    title         text NOT NULL,
    media_type    text NOT NULL,
    submitted_by  uuid NOT NULL,
    -- content-addressed, immutable. Never a mutable path.
    object_key    text NOT NULL UNIQUE
                  CHECK (object_key ~ '^[a-z0-9][a-z0-9/_.-]*$'),
    content_hash  text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    processing    processing_status NOT NULL DEFAULT 'queued',
    publication   publication_status NOT NULL DEFAULT 'draft',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE source_version (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id    uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    version_no   integer NOT NULL CHECK (version_no >= 1),
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, version_no)
);

CREATE TABLE fragment (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    ordinal   integer NOT NULL CHECK (ordinal >= 0),
    -- a printed label is NOT a file page; the two are distinct columns so the
    -- difference survives instead of being flattened into a fake page number
    locator_kind  locator_kind NOT NULL,
    file_page     integer CHECK (file_page IS NULL OR file_page >= 1),
    printed_label text,
    chapter       text,
    paragraph     integer CHECK (paragraph IS NULL OR paragraph >= 1),
    snapshot_url  text,
    snapshot_at   timestamptz,
    text          text NOT NULL,
    UNIQUE (source_id, ordinal),
    CONSTRAINT file_page_requires_kind CHECK (
        (locator_kind = 'pdf_file_page') = (file_page IS NOT NULL)
    ),
    CONSTRAINT printed_label_is_not_a_page CHECK (
        locator_kind <> 'pdf_printed_label' OR file_page IS NULL
    )
);

-- ------------------------------------------------------------ knowledge
CREATE TABLE knowledge (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id   uuid NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    statement    text NOT NULL,
    kind         text NOT NULL
                 CHECK (kind IN ('assertion','definition','recommendation','example')),
    verification verification_status NOT NULL DEFAULT 'unverified',
    publication  publication_status NOT NULL DEFAULT 'draft',
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_provenance (
    knowledge_id   uuid NOT NULL REFERENCES knowledge(id) ON DELETE CASCADE,
    source_id      uuid NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    fragment_id    uuid REFERENCES fragment(id) ON DELETE SET NULL,
    -- every field nullable ON PURPOSE: an unknown author is NULL, never "unknown"
    author          text,
    publication_ref text,
    url             text,
    retrieved_at    timestamptz,
    PRIMARY KEY (knowledge_id, source_id)
);

-- --------------------------------------------------------------- rules
-- A published rule is immutable. Correction is a new version_no, and the old
-- version stays readable because an experience already points at it.
CREATE TABLE rule (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id      uuid NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    version_no      integer NOT NULL CHECK (version_no >= 1),
    title           text NOT NULL,
    when_to_apply   text,
    expected_effect text,
    verification    text,
    publication     publication_status NOT NULL DEFAULT 'draft',
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (library_id, version_no),
    -- publishing requires the fields that make a rule applicable
    CONSTRAINT published_rule_is_applicable CHECK (
        publication <> 'published'
        OR (when_to_apply IS NOT NULL AND expected_effect IS NOT NULL)
    )
);

CREATE TABLE rule_action (
    rule_id  uuid NOT NULL REFERENCES rule(id) ON DELETE CASCADE,
    ordinal  integer NOT NULL CHECK (ordinal >= 0),
    body     text NOT NULL,
    PRIMARY KEY (rule_id, ordinal)
);

-- ---------------------------------------------------------- experience
CREATE TABLE use_record (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id         uuid NOT NULL REFERENCES rule(id) ON DELETE CASCADE,
    -- pinned to the exact version, never to "the current rule"
    rule_version_no integer NOT NULL,
    principal_id    uuid NOT NULL,
    state           usage_state NOT NULL DEFAULT 'retrieved',
    started_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE experience (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    use_id           uuid NOT NULL UNIQUE REFERENCES use_record(id) ON DELETE CASCADE,
    rule_id          uuid NOT NULL REFERENCES rule(id) ON DELETE CASCADE,
    rule_version_no  integer NOT NULL,
    where_applied    text,
    expected_result  text,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- An outcome is written only after something is reported. A metric has no
-- human author, so reported_by is nullable and rating_basis carries the
-- provenance of the rating instead.
CREATE TABLE outcome (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    use_id           uuid NOT NULL REFERENCES use_record(id) ON DELETE CASCADE,
    observed_result  text,
    rating           integer CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    rating_basis     text,
    reported_by      uuid,
    reported_at      timestamptz,
    is_correction    boolean NOT NULL DEFAULT false
);

-- ---------------------------------------------------------------- jobs
CREATE TABLE job (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id       uuid NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    kind             text NOT NULL
                     CHECK (kind IN ('ingest','extract','embed','compile','export')),
    status           processing_status NOT NULL DEFAULT 'queued',
    -- a retried submission must not create a second job
    idempotency_key  text NOT NULL,
    index_target     text,
    index_generation integer NOT NULL DEFAULT 1 CHECK (index_generation >= 1),
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (library_id, idempotency_key)
);

-- OpenViking is a rebuildable projection, never the source of truth.
CREATE TABLE index_generation (
    library_id  uuid NOT NULL REFERENCES library(id) ON DELETE CASCADE,
    generation  integer NOT NULL CHECK (generation >= 1),
    state       text NOT NULL CHECK (state IN ('building','current','retired')),
    canary_uri  text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    PRIMARY KEY (library_id, generation)
);

-- Generation is opt-in per library and billed to the owner's subscription.
-- A read never starts it.
CREATE TABLE generation_policy (
    library_id           uuid PRIMARY KEY REFERENCES library(id) ON DELETE CASCADE,
    generation_allowed   boolean NOT NULL DEFAULT false,
    max_concurrency      integer NOT NULL DEFAULT 1 CHECK (max_concurrency >= 1),
    requires_owner_start boolean NOT NULL DEFAULT true
);

COMMIT;
