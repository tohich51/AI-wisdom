-- 0004_uploads.sql — C10: the original-object layer.
--
-- Scope: C10. 0001, 0002 and 0003 are read by this card and are not touched.
--
-- Three things are added, and the reason for each is a failure this card is
-- required to survive:
--
--   kb.storage_backend   the routing registry. SCALING.md §5: a host absolute
--                        path is not a business id, so the database carries an
--                        object key plus a backend key. The backend row names
--                        an opaque *adapter* id; where those bytes actually live
--                        is a deployment fact, and it is not in the catalogue.
--                        One row at install, so nothing in the API is written
--                        in a way that would break when the second one appears.
--
--   kb.object_manifest   what is known about one immutable object: the source
--                        it backs, the byte size and the media type. Two
--                        libraries holding the same book have two manifests and
--                        ONE object key, which is what makes deduplication a
--                        storage decision instead of a permission decision.
--
--   kb.ingest            an attempt. A submission that dies half way leaves an
--                        ingest row in a non-committed state and nothing else:
--                        no source row, no manifest, and an object with no
--                        manifest is invisible to the product. This is the
--                        database-visible form of "обрыв не создаёт
--                        опубликованный source без blob".
--
-- Deliberately NOT added: a trigger.
--
-- The one invariant this layer would most like a trigger for is "a source may
-- not be readable without a manifest". Three other workers own a 0004_*.sql
-- with a different prefix on this same branch, and three card owners are
-- integrating the file; a trigger name is the one object that would collide
-- silently across them. The ordering invariant is therefore stated here and
-- enforced in the only place that can enforce it — the application, which
-- writes the source row and the manifest in one transaction and refuses to
-- hand back a commit marker until both are durable. That is the same order the
-- card asks for, and
-- tests/integration/upload/test_interrupted_upload.py::test_a_torn_upload_leaves_no_readable_source
-- proves it against a real server rather than asserting it in a comment.
--
-- Ordering within a single submission, and the reason for it:
--
--   1. bytes are written to the store under a content-addressed key
--   2. the source row is inserted
--   3. the manifest row is inserted   <-- the source becomes readable HERE
--   4. the ingest attempt is marked committed
--
-- A reader can only find a source through kb.source, and can only get bytes
-- for it through kb.object_manifest, so a crash before step 3 leaves an object
-- nobody can reach and a step-1 attempt row that the idempotency key will pick
-- up again on retry.

BEGIN;
SET search_path = kb, public;

-- ======================================================= storage backends
-- A row here is a routing decision, not a location. `key` is the stable
-- business identifier a caller may see; `adapter` is the implementation name
-- the gateway resolves internally. Neither column is a filesystem path, and
-- nothing in this card writes a path into the database — see SCALING.md §5:
-- "Абсолютный путь хоста не является бизнес-ID".
CREATE TABLE storage_backend (
    key        text PRIMARY KEY
              CHECK (key ~ '^[a-z][a-z0-9_-]{1,63}$'),
    title      text NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    -- opaque to the catalogue: 'local' now, 'object_store' later. Deliberately
    -- not a path and deliberately not a URL.
    adapter    text NOT NULL CHECK (adapter ~ '^[a-z][a-z0-9_-]{1,63}$'),
    is_default boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Seeded before RLS is enabled below, for the same reason 0003 seeds
-- kb.library_type there: FORCE ROW LEVEL SECURITY applies to the table owner
-- too, and this file runs as that owner. One row at install, so no API call
-- ever has to create a backend and the read path never has to cope with a
-- missing one.
INSERT INTO kb.storage_backend (key, title, adapter, is_default)
VALUES ('local', 'Local object store', 'local', true);

COMMENT ON TABLE kb.storage_backend IS
    'Routing registry for immutable originals. Contains adapter names, never '
    'host paths. A caller that can read this table learns which adapters the '
    'installation supports and nothing about anybody''s content.';

-- The key an object is written under is qualified by its backend, so a second
-- backend can never produce a colliding key for a different object.
--
-- The content hash is repeated here, and CHECKed against kb.source at insert
-- time by the application, because a manifest is the only thing that may answer
-- "is the stored byte still what the source claims it is?".

-- ====================================================== object manifests
CREATE TABLE object_manifest (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- THE link. The path from an object key to a readable source lives here and
    -- nowhere else, which is what makes "no manifest, no download" enforceable.
    object_key   text NOT NULL
                 CHECK (object_key ~ '^[a-z0-9][a-z0-9/_.-]*$')
                 REFERENCES kb.source(object_key) ON UPDATE RESTRICT,
    backend_key  text NOT NULL REFERENCES kb.storage_backend(key) ON UPDATE RESTRICT,
    source_id    uuid NOT NULL UNIQUE REFERENCES kb.source(id) ON DELETE CASCADE,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    byte_size    bigint NOT NULL CHECK (byte_size >= 0),
    -- What the bytes *are*, not what the uploader claimed and not what the
    -- fetcher's Content-Type said. 'application/octet-stream' when nothing could
    -- actually be determined: an honest unknown, never a guess.
    media_type   text NOT NULL CHECK (length(media_type) BETWEEN 1 AND 255),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (backend_key, object_key)
);

COMMENT ON TABLE kb.object_manifest IS
    'The retrievable copy of one immutable object. Without a row here a source '
    'has bytes on disk and no way to read them, and the download path refuses. '
    'Two libraries holding the same book have two manifests and one object_key.';

-- The manifest must describe the same bytes the source claims. A cross-table
-- CHECK is not expressible in PostgreSQL, so the two hashes are compared by
-- the writer inside the same transaction; the constraint below is the floor
-- that survives even a careless direct INSERT, and
-- test_upload_api.py::test_a_manifest_whose_hash_differs_from_its_source_is_refused
-- asserts the comparison itself.

-- ================================================================ ingest
-- One attempt at one submission. The idempotency key is supplied by the
-- caller, so a retried upload — a client that timed out and resent, a worker
-- that replayed its queue message — resolves to the row that already exists
-- instead of creating a second source.
--
-- `library_id` + `idempotency_key` is the unique pair. Note what is NOT
-- unique: `content_hash`. Two different callers in two different libraries
-- submitting the same bytes are two submissions, and the catalogue is not
-- allowed to tell either of them that the other one already has it
-- (ACCESS-MODEL §A20: "сообщение «такой файл есть у другого пользователя»
-- не раскрывается"). Deduplication happens in the permitted audience and
-- nowhere else.
CREATE TABLE ingest (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_id      uuid NOT NULL REFERENCES kb.library(id) ON DELETE CASCADE,
    idempotency_key text NOT NULL
                    CHECK (length(idempotency_key) BETWEEN 8 AND 200),
    -- a content hash is present exactly when bytes were actually received.
    -- A URL submission that was refused leaves NULL here, which is how "we
    -- never got any bytes" is distinguished from "the bytes were empty".
    content_hash    text CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),
    -- The URL this object was fetched from, or NULL when a file was uploaded.
    -- There is no default and no placeholder: a file upload genuinely has no
    -- URL, and saying "uploaded" in a text column would be a value pretending
    -- to be a reference.
    source_url      text,
    -- The instant the fetch actually returned, or NULL for a file upload.
    -- NOT the transaction time and NOT the row's created_at: this is the one
    -- place a "when was this retrieved" answer is allowed to come from, and it
    -- is NULL whenever nobody fetched anything.
    retrieved_at    timestamptz,
    state           text NOT NULL
                    CHECK (state IN ('received','fetched','stored','committed','abandoned')),
    -- The source this attempt produced. NULL until the attempt commits, and a
    -- committed attempt always has one: that pairing is the "no published
    -- source without a blob" invariant, stated as a row.
    source_id       uuid REFERENCES kb.source(id) ON DELETE SET NULL,
    submitted_by    uuid NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    committed_at    timestamptz,
    UNIQUE (library_id, idempotency_key),
    CONSTRAINT committed_ingest_has_a_source
        CHECK (state <> 'committed' OR source_id IS NOT NULL)
);

COMMENT ON TABLE kb.ingest IS
    'One submission attempt, keyed for idempotent retry. A committed row names '
    'the source it produced. Non-committed rows are the residue of an '
    'interrupted upload and describe no readable object.';

-- ================================================================= RLS
ALTER TABLE kb.storage_backend ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.storage_backend FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.object_manifest ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.object_manifest FORCE  ROW LEVEL SECURITY;
ALTER TABLE kb.ingest           ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb.ingest           FORCE  ROW LEVEL SECURITY;

-- ---------------------------------------------------------------- source_version
-- 0002 enables and FORCES RLS on kb.source_version and gives it exactly one
-- policy: source_version_read. There is no write policy, so kb_app cannot
-- INSERT a version at all — the table is default-deny in both directions.
--
-- That is a real gap rather than a design. "canonical source version" is one of
-- this card's outputs, and 0002's blanket grant of INSERT cannot be reached
-- because RLS has no policy to admit it. The proper fix belongs to the owner of
-- 0002; the narrow version of it is here, in the file that needs it:
--
--   * INSERT only. No UPDATE and no DELETE policy, so a version is as immutable
--     here as 0001 intended — a correction is a new version_no and the old row
--     stays for whatever points at it.
--   * contributor or better on the library of the source being versioned, the
--     same bar source_write already uses. A version is content about that
--     library's source and a reader cannot add one.
--   * the source it names must be visible to the caller, because the EXISTS
--     reads kb.source under that table's own FORCE RLS.
CREATE POLICY source_version_write ON kb.source_version FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM kb.source s
        WHERE s.id = source_id
          AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
    ));

-- The registry is a vocabulary of adapters, not user data. Reading it says
-- which backends exist, which is a deployment fact shown on an upload screen.
CREATE POLICY storage_backend_read ON kb.storage_backend FOR SELECT
    USING (kb.current_principal() IS NOT NULL);

-- A manifest inherits exactly the visibility of the source it backs, and adds
-- nothing of its own: the same EXISTS shape 0002 uses for kb.fragment. The
-- join is on object_key, which is UNIQUE in kb.source, so exactly one source —
-- and therefore exactly one library — can be behind a manifest.
CREATE POLICY object_manifest_read ON kb.object_manifest FOR SELECT
    USING (EXISTS (
        SELECT 1 FROM kb.source s
        WHERE s.object_key = object_key
          AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 10
    ));

-- Writing a manifest requires contributor on that same library, for the same
-- reason writing a source does. Without this a contributor could attach bytes
-- to a source in a library they cannot write to.
CREATE POLICY object_manifest_write ON kb.object_manifest FOR ALL
    USING      (EXISTS (
        SELECT 1 FROM kb.source s
        WHERE s.object_key = object_key
          AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM kb.source s
        WHERE s.object_key = object_key
          AND kb.role_rank(kb.effective_role(kb.current_principal(), s.library_id)) >= 20
    ));

-- An attempt is the submitter's own row, exactly like kb.use_record. It
-- carries a content hash, which is a fingerprint of a file; letting a reader
-- enumerate the attempts of a library would be a dedup oracle by another name.
CREATE POLICY ingest_own ON kb.ingest FOR ALL
    USING      (submitted_by = kb.current_principal())
    WITH CHECK (submitted_by = kb.current_principal());

-- =============================================================== grants
-- 0002's GRANT was one-time over the tables that existed then; these three did
-- not. Granted explicitly, and no wider than the policies above allow.
GRANT SELECT ON kb.storage_backend TO kb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb.object_manifest TO kb_app;
GRANT SELECT, INSERT, UPDATE ON kb.ingest TO kb_app;
-- kb.source_version already holds SELECT/INSERT/UPDATE/DELETE from 0002. The
-- grant was never the problem there; the missing policy above was.

-- The worker stores originals it was handed and reads them back. It is not
-- given the routing registry — the worker's contract is an object key handed
-- to it, not the ability to enumerate where objects live — and it is not
-- given the ingest table, which records who submitted what. C09's decision to
-- keep kb_worker narrow is not widened here.
GRANT SELECT ON kb.object_manifest TO kb_worker;

COMMIT;
