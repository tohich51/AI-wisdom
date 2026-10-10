"""C10 — submitting a source: file or URL, into an immutable original.

The shape of the whole card is in the ordering of the four steps below, so it is
worth stating before any of them:

    1. reserve the idempotency key      (own transaction, commits)
    2. put the bytes in the store       (no transaction, no locks held)
    3. create the source + the manifest (one transaction, commits together)
    4. mark the attempt committed       (same transaction as 3)

Why the bytes are written in step 2 and not at the end: a file write can be slow
and can fail, and holding a database transaction open across it would hold a row
lock on ``kb.ingest`` for the length of an upload. Why the source and the
manifest are created together in step 3: ``kb.object_manifest`` is the *only*
thing that turns bytes into something a reader may fetch, so as long as the two
rows share a commit there is no instant at which a readable source exists without
a blob. An interruption anywhere leaves a blob with no manifest — invisible — and
an attempt row that a retry resumes.

What this module refuses to do:

* **It never reads the caller's identity from the request.** It takes a
  :class:`Principal` the transport established, and the SQL runs as that
  principal under RLS. The request models below have no identity field at all.

* **It never reports a duplicate the caller may not see.** Deduplication is
  scoped to one library. The same book in two libraries is two sources, and the
  answer to the second library's upload says nothing whatsoever about the first.
  There is no "this file already exists somewhere" response, because that
  response *is* the A20 disclosure.

* **It never lets content do anything.** The submitted bytes are opaque. See
  ``kb.catalog.upload_data`` for the structural reason and for the framing
  helper, and ``tests/integration/upload/test_data_boundary.py`` for the proof.

* **It never treats a zero row count as success.** A write PostgreSQL filtered
  under RLS reports ``rowcount == 0`` and raises nothing (C06). Every insert here
  checks it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
from typing import BinaryIO, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import psycopg
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import AccessDenied, Principal, authorize, transaction_identity
from kb.catalog.storage import LocalBlobStore, ObjectNotFound, StorageError, object_key_for
from kb.catalog.upload_fetch import (
    FetchedObject,
    FetchPolicy,
    Resolver,
    UrlRefused,
    fetch_url,
    sniff_media_type,
)
from kb.contracts.entities import Source
from kb.contracts.enums import LibraryRole

# An upload is a stream, not a request body in memory. The ceiling is a
# deployment constant, never a per-request value, so a caller cannot raise its
# own limit by putting a number in the body.
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024

# The routing key. 'local' is seeded by migrations/0004_uploads.sql. It is a
# business identifier: the directory the bytes live in is not derived from it and
# never leaves kb.catalog.storage.
DEFAULT_BACKEND_KEY = "local"

# How many leading bytes are enough to identify a format. Every signature in
# kb.catalog.upload_fetch is eight bytes or shorter.
_SNIFF_BYTES = 8


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitFile(_Model):
    """A file submission.

    No ``principal_id``, no ``role``, no ``submitted_by``: identity comes from the
    transport (see :mod:`kb.http.uploads`), and ``extra="forbid"`` means a body
    carrying one is rejected with 422 before a handler runs.
    """

    library_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=200)


class FetchUrl(_Model):
    """The body of a URL submission.

    Separate from :class:`SubmitUrl` because the HTTP route takes the library
    from the path and the body must not be able to disagree with it. Two fields
    saying which library, one of which the caller can set, is a way to end up
    writing to a library the path never authorised.
    """

    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=200)


class SubmitUrl(FetchUrl):
    """A URL submission, addressed at one library.

    The URL is untrusted input. It is checked by ``kb.catalog.upload_fetch``
    before a socket exists, and it is never used to build a filesystem path.
    """

    library_id: UUID


class Original(_Model):
    """The retrievable copy. No host path, by construction.

    ``object_key`` is the business address (SCALING.md §5). ``backend`` is an
    adapter name. Neither is a location, and the model has no field that could
    become one.
    """

    source_id: UUID
    object_key: str
    backend: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    media_type: str


class SubmittedSource(_Model):
    """What a submission produced.

    ``status`` separates three outcomes that are not failures of each other: a
    first submission, an idempotent replay of one, and a second submission of
    bytes this library already holds.

    ``retrieved_at`` is None for a file upload. It is not the submission time, it
    is not the row's ``created_at``, and it is never a stand-in for either: there
    was no retrieval, so there is no value.
    """

    source: Source
    original: Original
    status: Literal["stored", "replayed", "deduplicated"]
    url: str | None = None
    retrieved_at: dt.datetime | None = None


class OriginalRead(_Model):
    """A Range read of one original, taken after the access check.

    ``complete`` is False when a Range was applied, so a caller can tell a whole
    object from a slice without comparing byte counts.
    """

    original: Original
    content: bytes
    complete: bool = True


# ------------------------------------------------------------------ internals


def _require_contributor(conn: psycopg.Connection, principal: Principal, library_id: UUID) -> None:
    """Refuse early, before a single byte is written, if the caller may not.

    PostgreSQL is still the authority — every INSERT below runs as this principal
    and RLS has the final say — but this check runs first so a reader cannot make
    the gateway write an object to the volume and then fail to register it. The
    blob would be unreferenced, and an unreferenced blob per request is a way to
    fill a disk with a role that is not allowed to store anything.

    It reads the caller's own grant row, which ``grant_read_own`` permits and
    which returns nothing for anybody else, so this cannot be used to enumerate a
    library's roster.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT role FROM kb.library_grant WHERE library_id = %s AND principal_id = %s",
            (library_id, principal.principal_id),
        )
        row = cur.fetchone()
    held = LibraryRole(row[0]) if row is not None else None
    authorize(
        principal,
        library_id,
        {} if held is None else {library_id: held},
        need=LibraryRole.CONTRIBUTOR,
    )


def _begin_attempt(
    conn: psycopg.Connection, principal: Principal, library_id: UUID, idempotency_key: str
) -> tuple[UUID, bool]:
    """Reserve the idempotency key.

    Returns ``(ingest_id, is_replay)``. ``is_replay`` is True only when the key
    was already committed — a key held by an *interrupted* attempt is resumed
    rather than replayed, which is the difference between "you already have this"
    and "you did not finish last time".

    A key held by somebody else is refused, and the enforcement is RLS rather
    than a comparison in this function: ``ingest_own`` makes a row visible only to
    its submitter, so the SELECT after a conflict returns nothing for a key
    somebody else holds. The unique index reports the conflict to the database
    even though the row stays invisible, and the answer given back is the same
    one a caller gets for a key that was never used. A guessed key therefore
    cannot be used to read another principal's source through the replay path.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb.ingest (library_id, idempotency_key, state, submitted_by)
            VALUES (%s, %s, 'received', %s)
            ON CONFLICT (library_id, idempotency_key) DO NOTHING
            """,
            (library_id, idempotency_key, principal.principal_id),
        )
        created = cur.rowcount == 1
        cur.execute(
            "SELECT id, state, source_id FROM kb.ingest "
            "WHERE library_id = %s AND idempotency_key = %s",
            (library_id, idempotency_key),
        )
        row = cur.fetchone()
    if row is None:
        if created:  # pragma: no cover - the insert we just made is visible to us
            raise RuntimeError("the attempt we just created is not visible to us")
        raise AccessDenied("this idempotency key belongs to another submission")
    return row[0], (not created) and row[1] == "committed" and row[2] is not None


def _commit(
    conn: psycopg.Connection,
    principal: Principal,
    ingest_id: UUID,
    *,
    library_id: UUID,
    title: str,
    media_type: str,
    object_key: str,
    content_hash: str,
    byte_size: int,
    source_url: str | None,
    retrieved_at: dt.datetime | None,
) -> SubmittedSource:
    """Source + manifest + a committed attempt, in one transaction, or none.

    Deduplication happens here, and only here, and only within ``library_id``.
    A second submission of the same bytes into the same library resolves to the
    source that already holds them, with no new source row and no new manifest —
    ``kb.object_manifest.source_id`` is UNIQUE, so a duplicate manifest could not
    be written even by accident.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM kb.source WHERE library_id = %s AND content_hash = %s LIMIT 1",
            (library_id, content_hash),
        )
        existing = cur.fetchone()
        if existing is not None:
            source_id: UUID = existing[0]
            status: Literal["stored", "replayed", "deduplicated"] = "deduplicated"
        else:
            cur.execute(
                """
                INSERT INTO kb.source
                    (library_id, title, media_type, submitted_by, object_key, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (library_id, title, media_type, principal.principal_id, object_key, content_hash),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover - RETURNING always yields a row
                raise RuntimeError("the source insert returned no id")
            source_id = row[0]
            status = "stored"
            # The manifest is the only path from an object key to a readable
            # source, and it is written here in the same transaction as the
            # source row. The WHERE clause re-reads the source inside the
            # database: if the hashes disagree, or if RLS filtered the row, no
            # manifest is inserted and rowcount is 0 — which is a failure, not a
            # success.
            cur.execute(
                """
                INSERT INTO kb.object_manifest
                    (object_key, backend_key, source_id, content_hash, byte_size, media_type)
                SELECT %s, %s, %s, %s, %s, %s
                FROM kb.source s
                WHERE s.id = %s AND s.object_key = %s AND s.content_hash = %s
                """,
                (
                    object_key,
                    DEFAULT_BACKEND_KEY,
                    source_id,
                    content_hash,
                    byte_size,
                    media_type,
                    source_id,
                    object_key,
                    content_hash,
                ),
            )
            if cur.rowcount != 1:
                raise AccessDenied("the original could not be attached to the source")
            # The canonical first version of this source. version_no starts at 1
            # and only ever increases; it is never rewritten.
            cur.execute(
                "INSERT INTO kb.source_version (source_id, version_no, content_hash) "
                "VALUES (%s, 1, %s) ON CONFLICT (source_id, version_no) DO NOTHING",
                (source_id, content_hash),
            )
        cur.execute(
            "UPDATE kb.ingest SET state = 'committed', source_id = %s, committed_at = now(), "
            "content_hash = COALESCE(content_hash, %s), "
            "source_url = COALESCE(source_url, %s), "
            "retrieved_at = COALESCE(retrieved_at, %s) "
            "WHERE id = %s AND submitted_by = %s",
            (source_id, content_hash, source_url, retrieved_at, ingest_id, principal.principal_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("the ingest attempt could not be committed")

    described = _describe(conn, principal, source_id)
    if described is None:  # pragma: no cover - the source was just written
        raise RuntimeError("the source is invisible to the principal that wrote it")
    source, original, stored_url, stored_at = described
    return SubmittedSource(
        source=source,
        original=original,
        status=status,
        url=stored_url,
        retrieved_at=stored_at,
    )


def _describe(
    conn: psycopg.Connection, principal: Principal, source_id: UUID
) -> tuple[Source, Original, str | None, dt.datetime | None] | None:
    """The stored answer for one source, or None when the caller may not see it.

    Three RLS-scoped reads joined by clauses that widen none of them. A caller who
    cannot read the source cannot read the manifest either — the manifest's
    policy is the same ``EXISTS`` over ``kb.source`` that 0002 uses for
    ``kb.fragment`` — so revoking a grant hides both in the same commit.

    A source whose manifest is missing returns None. That is the half of the
    invariant that a reader can check, and it is what makes "no blob, no
    download" true rather than aspirational.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT s.id, s.library_id, s.title, s.submitted_by, s.media_type, s.object_key, "
            "s.content_hash, s.processing, s.publication, s.created_at, "
            "m.backend_key, m.byte_size, m.media_type "
            "FROM kb.source s JOIN kb.object_manifest m ON m.source_id = s.id "
            "WHERE s.id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    # The submission provenance is read separately and is *expected* to be
    # missing for a source that did not come from a submission. Absence is the
    # honest answer and is carried through as None.
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT source_url, retrieved_at FROM kb.ingest WHERE source_id = %s "
            "ORDER BY created_at LIMIT 1",
            (source_id,),
        )
        provenance = cur.fetchone()
    source = Source(
        id=row[0],
        library_id=row[1],
        title=row[2],
        submitted_by=row[3],
        media_type=row[4],
        object_key=row[5],
        content_hash=row[6],
        processing=row[7],
        publication=row[8],
        created_at=row[9],
    )
    original = Original(
        source_id=row[0],
        object_key=row[5],
        backend=row[10],
        content_hash=row[6],
        byte_size=int(row[11]),
        media_type=row[12],
    )
    url = provenance[0] if provenance else None
    retrieved = provenance[1] if provenance else None
    return source, original, url, retrieved


def _object_key(library_id: UUID, content_hash: str) -> str:
    """The content-addressed key for these bytes in this library.

    Scoped by library on purpose; see ``object_key_for`` for why, and for the
    DDL proposal that would let the scope go away.
    """
    return object_key_for(content_hash, scope=library_id.hex)


def _media_type_of(store: LocalBlobStore, object_key: str, declared: str | None) -> str:
    """The media type, read from the stored bytes rather than the caller's claim.

    Reading the object back that was just written is deliberate: the answer is
    about what is *on disk*, which is what every later reader will get, and not
    about what a multipart part said it was sending.
    """
    head = store.read_range(object_key, 0, _SNIFF_BYTES)
    return sniff_media_type(head, declared)


def _title_for(spec_title: str | None, fallback: str) -> str:
    """A title that is either the caller's or a fact, never a placeholder.

    There is no "Untitled" and no "upload". If the caller named the source, that
    is the title. If not, the title is the last path segment of the URL that was
    actually fetched, or the object key — both of which are true statements about
    what arrived, which is the only kind of default this contract accepts.
    """
    if spec_title:
        return spec_title
    segment = urlsplit(fallback).path.rstrip("/").rsplit("/", 1)[-1]
    return segment or fallback


# ------------------------------------------------------------------ submission


def submit_file(
    conn: psycopg.Connection,
    principal: Principal,
    store: LocalBlobStore,
    spec: SubmitFile,
    stream: BinaryIO,
    *,
    declared_media_type: str | None = None,
    filename: str | None = None,
    max_bytes: int | None = None,
) -> SubmittedSource:
    """Stream a file into the store and register it as a source.

    The stream is consumed exactly once, straight into the store. Nothing here
    buffers the whole body, and nothing here looks at the content: the bytes are
    hashed, counted and written, and that is all that happens to them.
    """
    _require_contributor(conn, principal, spec.library_id)
    ingest_id, is_replay = _begin_attempt(conn, principal, spec.library_id, spec.idempotency_key)
    if is_replay:
        return _replay(conn, principal, ingest_id)

    stored = store.put(
        stream,
        media_type=(declared_media_type or "application/octet-stream"),
        scope=spec.library_id.hex,
        max_bytes=max_bytes or DEFAULT_MAX_UPLOAD_BYTES,
    )
    media_type = _media_type_of(store, stored.object_key, declared_media_type)
    return _commit(
        conn,
        principal,
        ingest_id,
        library_id=spec.library_id,
        title=_title_for(spec.title, filename or stored.object_key),
        media_type=media_type,
        object_key=stored.object_key,
        content_hash=stored.content_hash,
        byte_size=stored.size,
        source_url=None,
        # A file upload has no retrieval. The column stays NULL; it is never
        # filled with the submission time to look complete.
        retrieved_at=None,
    )


def submit_url(
    conn: psycopg.Connection,
    principal: Principal,
    store: LocalBlobStore,
    spec: SubmitUrl,
    *,
    policy: FetchPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    max_bytes: int | None = None,
) -> SubmittedSource:
    """Fetch a URL under the SSRF policy and register what came back.

    The fetch happens *before* the attempt is reserved, and that order is
    deliberate. A refused URL — loopback, a metadata address, a redirect to one,
    too many hops, a ``file:`` — leaves no trace at all: no attempt row, no
    content hash, nothing a later reader could mistake for a failed ingest of
    content that does not exist. A refusal is an error, and an error is not a row.

    ``transport`` and ``resolver`` exist so the redirect and rebinding cases can
    be driven deterministically over a real httpx client. In the product both are
    None: the real resolver and the real transport are used.
    """
    _require_contributor(conn, principal, spec.library_id)
    fetched: FetchedObject = fetch_url(
        spec.url, policy or FetchPolicy(), transport=transport, resolver=resolver
    )
    ceiling = max_bytes or DEFAULT_MAX_UPLOAD_BYTES
    if len(fetched.content) > ceiling:
        raise StorageError("the fetched object is larger than the configured upload limit")

    ingest_id, is_replay = _begin_attempt(conn, principal, spec.library_id, spec.idempotency_key)
    if is_replay:
        return _replay(conn, principal, ingest_id)

    # The retrieval time is measured here, at the moment the bytes came back. It
    # is not the transaction clock, and it is not invented.
    retrieved_at = _now()
    stored = store.put(
        io.BytesIO(fetched.content),
        media_type=fetched.media_type,
        scope=spec.library_id.hex,
        max_bytes=ceiling,
    )
    return _commit(
        conn,
        principal,
        ingest_id,
        library_id=spec.library_id,
        title=_title_for(spec.title, fetched.final_url),
        media_type=fetched.media_type,
        object_key=stored.object_key,
        content_hash=stored.content_hash,
        byte_size=stored.size,
        source_url=fetched.final_url,
        retrieved_at=retrieved_at,
    )


def _replay(conn: psycopg.Connection, principal: Principal, ingest_id: UUID) -> SubmittedSource:
    """Answer a repeated submission with the source it already produced.

    No fetch, no write, no second source. The answer is the same source the first
    submission returned, and it is only ever returned to the principal that made
    that submission.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute("SELECT source_id FROM kb.ingest WHERE id = %s", (ingest_id,))
        row = cur.fetchone()
    if row is None or row[0] is None:  # pragma: no cover - committed implies a source
        raise RuntimeError("a committed attempt carries no source")
    described = _describe(conn, principal, row[0])
    if described is None:  # pragma: no cover - the principal wrote it
        raise RuntimeError("the source is invisible to the principal that wrote it")
    source, original, url, retrieved = described
    return SubmittedSource(
        source=source, original=original, status="replayed", url=url, retrieved_at=retrieved
    )


# --------------------------------------------------------------------- reading


def describe_original(
    conn: psycopg.Connection, principal: Principal, source_id: UUID
) -> OriginalRead | None:
    """The manifest for one source, without touching the bytes.

    ``None`` means "not visible to you", which is also what "does not exist"
    means here. Exposed so a Range request can be validated against the real size
    before any data moves, without a throwaway read.
    """
    described = _describe(conn, principal, source_id)
    if described is None:
        return None
    _source, original, _url, _retrieved = described
    return OriginalRead(original=original, content=b"", complete=True)


def read_original(
    conn: psycopg.Connection,
    principal: Principal,
    store: LocalBlobStore,
    source_id: UUID,
    *,
    start: int | None = None,
    stop: int | None = None,
) -> OriginalRead:
    """Read an original, or a byte range of one, after the access check.

    The order is access first, bytes second. A caller with no grant never reaches
    ``store.open``: RLS has already returned no row, and the answer is the same
    404-shaped refusal as for a source that does not exist. There is no signed
    URL, no pre-signed Range and no long-lived handle — SCALING.md §5 is explicit
    that "выдача долгоживущего прямого URL не должна обходить последующий отзыв
    прав", so every read comes back through here and re-checks.

    A whole-object read re-hashes what it read and compares it with the
    content-addressed key. A mismatch is reported rather than served: the bytes
    are not what the catalogue says they are, and handing them out as if they
    were would put fabricated provenance into a reader's hands.
    """
    described = _describe(conn, principal, source_id)
    if described is None:
        raise AccessDenied("no such source")
    _source, original, _url, _retrieved = described
    if not store.exists(original.object_key):
        # The row and the blob have drifted apart. Refuse loudly.
        raise ObjectNotFound(f"no stored object for source {source_id}")

    if start is None and stop is None:
        with store.open(original.object_key) as handle:
            content = handle.read()
        if hashlib.sha256(content).hexdigest() != original.content_hash:
            raise StorageError(
                "the stored bytes do not match the source's content hash; refusing to serve them"
            )
        return OriginalRead(original=original, content=content, complete=True)

    begin = start or 0
    end = stop
    if end is None:
        end = begin + original.byte_size
    return OriginalRead(
        original=original,
        content=store.read_range(original.object_key, begin, end),
        complete=False,
    )


__all__ = [
    "DEFAULT_BACKEND_KEY",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "FetchUrl",
    "ObjectNotFound",
    "Original",
    "OriginalRead",
    "StorageError",
    "SubmitFile",
    "SubmitUrl",
    "SubmittedSource",
    "UrlRefused",
    "describe_original",
    "read_original",
    "submit_file",
    "submit_url",
]
