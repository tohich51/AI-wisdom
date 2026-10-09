-- C04 spike scaffold: two separate OpenViking accounts, one per library.
-- NOT EXECUTED in this environment. See docs/handoff/results/C04.json for the
-- exact blocker. This file is the schema the runtime would use, kept honest by
-- having no "it works" marker in it.

-- PRODUCT-SPEC: separate OpenViking account per library, explicit service
-- identities, restricted ACL. The gateway never receives a root provisioning
-- secret, and there are never two writing OV processes in one local workspace.

CREATE SCHEMA IF NOT EXISTS index_spike;

CREATE TABLE IF NOT EXISTS index_spike.library_account (
    library_id        uuid PRIMARY KEY,
    ov_account        text NOT NULL UNIQUE,   -- e.g. "kb-lib-<uuid>"
    service_identity  text NOT NULL,          -- explicit, not shared
    acl               jsonb NOT NULL,          -- restricted per library
    dimension         integer NOT NULL,        -- 1024 for the CPU model
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- OpenViking is a rebuildable projection, not the source of truth. Every
-- rebuild bumps the generation, so a half-finished reindex is detectable
-- instead of silently serving mixed generations.
CREATE TABLE IF NOT EXISTS index_spike.index_generation (
    library_id     uuid NOT NULL REFERENCES index_spike.library_account(library_id),
    generation     integer NOT NULL CHECK (generation >= 1),
    state          text NOT NULL CHECK (state IN ('building', 'current', 'retired')),
    canary_uri     text,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    PRIMARY KEY (library_id, generation)
);

-- Idempotent reindex: re-running with the same content hash must not create a
-- second generation. This is the check for PRODUCT-SPEC acceptance #3.
CREATE TABLE IF NOT EXISTS index_spike.index_idempotency (
    library_id    uuid NOT NULL,
    generation    integer NOT NULL,
    content_hash  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (library_id, content_hash)
);

-- Ordinary search must not generate. Generation is opt-in per library and
-- billed to the owner's subscription, never silently triggered by a read.
CREATE TABLE IF NOT EXISTS index_spike.generation_policy (
    library_id            uuid PRIMARY KEY,
    generation_allowed    boolean NOT NULL DEFAULT false,
    max_concurrency       integer NOT NULL DEFAULT 1,
    requires_owner_start  boolean NOT NULL DEFAULT true
);
