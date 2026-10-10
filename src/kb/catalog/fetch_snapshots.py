"""C12B — recording one HTML snapshot in the catalogue as an immutable source.

The fetch (:mod:`kb.catalog.fetch_html`) decides whether a URL may be read; the
extraction (:mod:`kb.catalog.parsers.html_extract`) decides what the page says;
this module decides what the catalogue *records* about it, and it records four
facts and nothing more:

* the URL the bytes actually came from, and the instant they arrived;
* the raw bytes, content-addressed and immutable;
* the versioned fragment set, each fragment addressed by snapshot URL, snapshot
  time and its own ordinal;
* the extractor version that produced those fragments.

Three rules are enforced here rather than left to the caller, and each is a test:

**Identity comes from the transport.** The functions below take a
:class:`kb.access.policy.Principal` the transport established and run their SQL
under ``app.principal`` as that principal. :class:`HtmlIngestSpec` has
``extra="forbid"`` and carries no identity field at all, so a body containing
``submitted_by`` is a 422 before a handler runs, and the row's ``submitted_by``
is whatever the verified token said.

**A refused page leaves nothing.** The fetch happens before this module is
called, so a loopback URL, a redirect to a private address or a ``file:`` never
reaches a transaction. There is no attempt row, no content hash and no object —
an error is not a row.

**A page cannot act.** The bytes go into a store, the text goes into
``kb.fragment``, and nothing in this module evaluates, imports, renders or
dispatches anything the page contained. The fragment rows are inserted with
bound parameters; the text is a value, not a statement. That is a structural
property, and ``tests/integration/fetch/test_data_boundary.py`` proves it over
the parsed AST rather than over this docstring.

Where the schema cannot carry what the product wants, this module says so out
loud instead of improvising. ``kb.fragment`` has no version column, and
``kb.source_version`` has no object key, so a *changed* page re-fetched over an
existing source produces a new, honest version row while the first version's
fragments stay exactly as they were. The result carries the limitation; it does
not claim the new page's text is in the catalogue. The proposed DDL is in the
card's result file for the single owner to apply.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import AccessDenied, Principal, authorize, transaction_identity
from kb.catalog.fetch_html import HtmlSnapshot, normalise_for_display
from kb.catalog.parsers.html_extract import ParsedDocument
from kb.catalog.storage import LocalBlobStore, StorageError, object_key_for
from kb.contracts.entities import Fragment
from kb.contracts.enums import LibraryRole, ProcessingStatus

# The routing key. 'local' is seeded by migrations/0004_uploads.sql; the same
# constant C10 uses, because there is one object store in this installation.
DEFAULT_BACKEND_KEY = "local"

# The media type recorded for an extracted page. Kept as a constant so a caller
# cannot make a fetch of a PDF claim to be HTML.
HTML_MEDIA_TYPE = "text/html"

# A second ceiling on the way in. The fetch layer already caps a page at 8 MiB;
# this one is here so that a snapshot arriving from anywhere else — a replayed
# queue message, a future caller — is still bounded before it reaches a disk.
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024


class HtmlIngestSpec(BaseModel):
    """What a caller may say about a snapshot submission.

    No ``principal_id``, no ``role``, no ``submitted_by``: identity comes from
    the transport (see :mod:`kb.http.uploads` for the C10 route that does it),
    and ``extra="forbid"`` means a body carrying one is rejected before a
    handler runs.

    ``source_id`` addresses an *existing* snapshot to fetch again. It is what
    makes a second retrieval a new version of one source rather than a second
    unrelated source.
    """

    model_config = ConfigDict(extra="forbid")

    library_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=300)
    idempotency_key: str = Field(min_length=8, max_length=200)
    source_id: UUID | None = None


@dataclass(frozen=True)
class HtmlIngestResult:
    """What the catalogue now holds, and everything that is still missing.

    ``status`` is one of:

    * ``stored`` — a new source, its original, version 1 and its fragments;
    * ``versioned`` — an existing source gained a new version row. The bytes of
      that version are *not* downloadable: ``kb.source_version`` has no object
      key, which is a schema limitation and is named in ``limitations``;
    * ``unchanged`` — the page is byte-identical to the current version. No new
      version, because a version that changes nothing is not a version;
    * ``deduplicated`` — this library already holds these exact bytes. No second
      source, and the answer names nothing outside this library (A20);
    * ``replayed`` — the same idempotency key came back. No fetch happened in
      this call and no row was written.
    """

    source_id: UUID
    status: str
    version_no: int
    url: str
    retrieved_at: dt.datetime
    content_hash: str
    object_key: str | None
    byte_size: int
    media_type: str
    extractor_version: str
    fragments: tuple[Fragment, ...]
    fragment_count: int
    detection_count: int
    truncated: bool
    limitations: tuple[str, ...] = ()

    @property
    def is_readable(self) -> bool:
        """Whether these exact bytes can be fetched back from the store.

        False for a version recorded without a manifest. Reporting it is the
        point: a caller must never read "versioned" as "the new page is
        available".
        """
        return self.object_key is not None


def _one(cur: psycopg.Cursor) -> Any:
    """One row, or an error.

    ``fetchone`` returns ``None`` for "no row", and a caller that indexes it
    anyway gets a ``TypeError`` from deep inside a persistence function. The
    COUNT and MAX queries below always answer, so a missing row means something
    is wrong with the schema, and saying so is better than guessing.
    """
    row = cur.fetchone()
    if row is None:  # pragma: no cover - every query using this returns exactly one row
        raise RuntimeError("the query returned no row")
    return row


def _require_contributor(conn: psycopg.Connection, principal: Principal, library_id: UUID) -> None:
    """Refuse before a byte is written if the caller may not store anything.

    PostgreSQL is still the authority — every INSERT below runs under this
    principal and RLS has the last word — but this runs first so a reader cannot
    make the gateway write an object to the volume and then fail to register it.
    An unregistered object per request is a way to fill a disk with a role that
    is not allowed to store anything.

    It reads the caller's own grant row, which ``grant_read_own`` permits and
    which returns nothing for anybody else, so it cannot enumerate a library.
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


def _check_pair(snapshot: HtmlSnapshot, parsed: ParsedDocument) -> None:
    """Refuse a fragment set that was not extracted from this snapshot.

    A caller that can pass a ``ParsedDocument`` is a caller that can attach one
    page's text to another page's provenance. The three fields that make a
    snapshot a snapshot are compared, and a mismatch is an error rather than a
    best guess.
    """
    if parsed.content_hash != snapshot.content_hash:
        raise StorageError("the fragment set was extracted from different bytes")
    if normalise_for_display(parsed.url) != normalise_for_display(snapshot.final_url):
        raise StorageError("the fragment set was extracted from a different URL")
    if parsed.retrieved_at != snapshot.retrieved_at:
        raise StorageError("the fragment set carries a different retrieval instant")
    if snapshot.byte_size > MAX_SNAPSHOT_BYTES:
        raise StorageError(f"a snapshot may not exceed {MAX_SNAPSHOT_BYTES} bytes")


def _begin_attempt(
    conn: psycopg.Connection, principal: Principal, spec: HtmlIngestSpec
) -> tuple[UUID, bool]:
    """Reserve the idempotency key. Returns ``(ingest_id, is_replay)``.

    A key already held by somebody else is refused, and the enforcement is RLS
    rather than a comparison here: ``ingest_own`` makes the row visible only to
    its submitter, so the SELECT after a conflict returns nothing and the
    answer is the one a caller gets for a key that was never used.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kb.ingest (library_id, idempotency_key, state, submitted_by) "
            "VALUES (%s, %s, 'fetched', %s) ON CONFLICT (library_id, idempotency_key) DO NOTHING",
            (spec.library_id, spec.idempotency_key, principal.principal_id),
        )
        created = cur.rowcount == 1
        cur.execute(
            "SELECT id, state, source_id FROM kb.ingest "
            "WHERE library_id = %s AND idempotency_key = %s",
            (spec.library_id, spec.idempotency_key),
        )
        row = cur.fetchone()
    if row is None:
        if created:  # pragma: no cover - the row we just wrote is visible to us
            raise RuntimeError("the attempt we just created is not visible to us")
        raise AccessDenied("this idempotency key belongs to another submission")
    return row[0], (not created) and row[1] == "committed" and row[2] is not None


def _fragments(source_id: UUID, parsed: ParsedDocument) -> tuple[Fragment, ...]:
    return tuple(
        Fragment(
            id=_fragment_id(source_id, fragment.ordinal),
            source_id=source_id,
            ordinal=fragment.ordinal,
            locator=fragment.locator(parsed.url, parsed.retrieved_at),
            text=fragment.text,
        )
        for fragment in parsed.fragments
    )


def _fragment_id(source_id: UUID, ordinal: int) -> UUID:
    """A deterministic id from the source id and the ordinal.

    Derived, not generated, so a retried insert of the same fragment set produces
    the same rows instead of a second copy of the page. This is a UUID-shaped
    derivation for idempotence, not a security token: it is visible to anybody
    who can read the fragment.
    """
    return uuid.uuid5(source_id, f"html-fragment-{ordinal}")


def _write_fragments(
    cur: psycopg.Cursor,
    source_id: UUID,
    fragments: tuple[Fragment, ...],
) -> None:
    """Insert the fragment set, and prove that all of it landed.

    A write PostgreSQL filtered under RLS reports ``rowcount == 0`` and raises
    nothing, so a silent "stored" answer for a page whose text never reached the
    catalogue is possible if rowcount is not checked. It is checked here, for
    the batch and again for the whole set.
    """
    if not fragments:
        return
    cur.executemany(
        "INSERT INTO kb.fragment (id, source_id, ordinal, locator_kind, paragraph, "
        "chapter, snapshot_url, snapshot_at, text) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [
            (
                fragment.id,
                fragment.source_id,
                fragment.ordinal,
                str(fragment.locator.kind),
                fragment.locator.paragraph,
                fragment.locator.chapter,
                fragment.locator.snapshot_url,
                fragment.locator.snapshot_at,
                fragment.text,
            )
            for fragment in fragments
        ],
    )
    if cur.rowcount != len(fragments):
        raise AccessDenied("the fragment set was refused by the database")
    cur.execute("SELECT count(*) FROM kb.fragment WHERE source_id = %s", (source_id,))
    stored = int(_one(cur)[0])
    if stored != len(fragments):  # pragma: no cover - same transaction, so this cannot drift
        raise AccessDenied(f"expected {len(fragments)} fragments, the database holds {stored}")


def _commit_new_source(
    conn: psycopg.Connection,
    principal: Principal,
    spec: HtmlIngestSpec,
    ingest_id: UUID,
    snapshot: HtmlSnapshot,
    parsed: ParsedDocument,
    object_key: str,
) -> tuple[UUID, str, str, int]:
    """Source, manifest, version 1, fragments and a committed attempt, or none.

    They share one transaction, so there is no instant at which a readable
    source exists without a blob, and none at which a page's text exists without
    the snapshot it came from. Deduplication is scoped to ``library_id`` and is
    invisible across libraries: the same page in two libraries is two sources,
    and neither submission is told the other exists.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM kb.source WHERE library_id = %s AND content_hash = %s LIMIT 1",
            (spec.library_id, snapshot.content_hash),
        )
        existing = cur.fetchone()
        if existing is not None:
            source_id: UUID = existing[0]
            status = "deduplicated"
        else:
            cur.execute(
                """
                INSERT INTO kb.source
                    (library_id, title, media_type, submitted_by, object_key, content_hash,
                     processing, publication)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft')
                RETURNING id
                """,
                (
                    spec.library_id,
                    spec.title or _title_from_url(snapshot.final_url),
                    HTML_MEDIA_TYPE,
                    principal.principal_id,
                    object_key,
                    snapshot.content_hash,
                    str(ProcessingStatus.QUEUED),
                ),
            )
            row = cur.fetchone()
            if row is None:  # pragma: no cover - RETURNING always yields a row
                raise RuntimeError("the source insert returned no id")
            source_id = row[0]
            status = "stored"
            # The manifest is the only path from an object key to a readable
            # source. The WHERE re-reads the source inside the database: if the
            # hashes disagree, or if RLS filtered the row, no manifest is
            # inserted and rowcount is 0 — which is a failure, not a success.
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
                    snapshot.content_hash,
                    snapshot.byte_size,
                    HTML_MEDIA_TYPE,
                    source_id,
                    object_key,
                    snapshot.content_hash,
                ),
            )
            if cur.rowcount != 1:
                raise AccessDenied("the original could not be attached to the source")
            cur.execute(
                "INSERT INTO kb.source_version (source_id, version_no, content_hash) "
                "VALUES (%s, 1, %s) ON CONFLICT (source_id, version_no) DO NOTHING",
                (source_id, snapshot.content_hash),
            )
            if cur.rowcount != 1:  # pragma: no cover - ON CONFLICT DO NOTHING hides a replay
                raise StorageError("the first version of this source already exists")
            _write_fragments(cur, source_id, _fragments(source_id, parsed))
        cur.execute(
            "UPDATE kb.ingest SET state = 'committed', source_id = %s, committed_at = now(), "
            "content_hash = COALESCE(content_hash, %s), source_url = %s, retrieved_at = %s "
            "WHERE id = %s AND submitted_by = %s",
            (
                source_id,
                snapshot.content_hash,
                snapshot.final_url,
                snapshot.retrieved_at,
                ingest_id,
                principal.principal_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("the ingest attempt could not be committed")
    return source_id, status, snapshot.content_hash, snapshot.byte_size


def _title_from_url(url: str) -> str:
    """A title that is a fact about what arrived, never a placeholder.

    The last path segment of the URL that was actually fetched. It is true, and
    an operator can correct it; "Untitled" would be a value pretending to be a
    fact.
    """
    segment = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return segment or url


def _existing_version(
    conn: psycopg.Connection, principal: Principal, source_id: UUID
) -> tuple[UUID, int, str] | None:
    """``(library_id, version_no, content_hash)`` of the current version.

    Read under the caller's identity, so a source in a library the caller cannot
    read returns None and the answer is the same as for a source that does not
    exist. That is the A20 disclosure rule applied to a re-fetch.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT s.library_id, v.version_no, v.content_hash "
            "FROM kb.source s JOIN kb.source_version v ON v.source_id = s.id "
            "WHERE s.id = %s ORDER BY v.version_no DESC LIMIT 1",
            (source_id,),
        )
        row = cur.fetchone()
    return (row[0], int(row[1]), row[2]) if row is not None else None


def _commit_new_version(
    conn: psycopg.Connection,
    principal: Principal,
    spec: HtmlIngestSpec,
    ingest_id: UUID,
    source_id: UUID,
    snapshot: HtmlSnapshot,
) -> int:
    """Add a version to an existing source, and only that.

    No blob is written. ``kb.object_manifest`` allows one manifest per source and
    ``kb.source_version`` has nowhere to name an object, so storing these bytes
    would create a file no reader could reach — unreferenced residue, which is
    what C10 documented and this card does not add to. The version row is the
    fact: this URL, this instant, this content hash, and version 1 untouched.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT max(version_no) FROM kb.source_version WHERE source_id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        current = int(row[0]) if row and row[0] is not None else 0
        version_no = current + 1
        cur.execute(
            "INSERT INTO kb.source_version (source_id, version_no, content_hash) "
            "VALUES (%s, %s, %s)",
            (source_id, version_no, snapshot.content_hash),
        )
        if cur.rowcount != 1:
            raise AccessDenied("the new version was refused by the database")
        cur.execute(
            "UPDATE kb.ingest SET state = 'committed', source_id = %s, committed_at = now(), "
            "content_hash = %s, source_url = %s, retrieved_at = %s "
            "WHERE id = %s AND submitted_by = %s",
            (
                source_id,
                snapshot.content_hash,
                snapshot.final_url,
                snapshot.retrieved_at,
                ingest_id,
                principal.principal_id,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("the ingest attempt could not be committed")
    return version_no


_LIMITATION_FRAGMENTS = (
    "kb.fragment has no version column, so the fragments of the new version are not "
    "stored and the previous version's fragments are unchanged. Proposed DDL: add "
    "version_no to kb.fragment and an INSERT-only write policy, as 0004_uploads.sql "
    "does for kb.source_version."
)
_LIMITATION_BYTES = (
    "kb.source_version has no object key and kb.object_manifest allows one manifest per "
    "source, so the bytes of this version are not stored and this version is not "
    "downloadable. Proposed DDL: an object key on kb.source_version with its own "
    "manifest row."
)


def ingest_html_snapshot(
    conn: psycopg.Connection,
    principal: Principal,
    store: LocalBlobStore,
    snapshot: HtmlSnapshot,
    parsed: ParsedDocument,
    spec: HtmlIngestSpec,
) -> HtmlIngestResult:
    """Record one fetched page: the original, the version, and the fragments.

    The caller has already fetched and extracted — this function assumes it
    holds a :class:`HtmlSnapshot` and the :class:`ParsedDocument` made from that
    exact snapshot, and says so if it does not. The security boundary was crossed
    before either of them existed; nothing here re-opens it, and nothing here
    can widen it either, because this module never opens a socket.
    """
    _require_contributor(conn, principal, spec.library_id)
    _check_pair(snapshot, parsed)

    if spec.source_id is not None:
        return _ingest_as_new_version(conn, principal, store, snapshot, parsed, spec)

    ingest_id, is_replay = _begin_attempt(conn, principal, spec)
    if is_replay:
        return _replay(conn, principal, ingest_id)

    # The object key is a pure function of the content hash and the library. The
    # URL never appears in it, so no page can influence where its own bytes are
    # written.
    object_key = object_key_for(snapshot.content_hash, scope=spec.library_id.hex)
    stored = store.put(
        io.BytesIO(snapshot.content),
        media_type=HTML_MEDIA_TYPE,
        scope=spec.library_id.hex,
        max_bytes=MAX_SNAPSHOT_BYTES,
    )
    if stored.object_key != object_key:  # pragma: no cover - both derive from the digest
        raise StorageError("the store returned a key that is not the content address")
    source_id, status, content_hash, byte_size = _commit_new_source(
        conn, principal, spec, ingest_id, snapshot, parsed, stored.object_key
    )
    fragments = _fragments(source_id, parsed) if status == "stored" else ()
    limitations: tuple[str, ...] = ()
    if status == "deduplicated":
        limitations = (
            "this library already holds these exact bytes; no second source and no "
            "second fragment set were created",
        )
    return HtmlIngestResult(
        source_id=source_id,
        status=status,
        version_no=1,
        url=snapshot.final_url,
        retrieved_at=snapshot.retrieved_at,
        content_hash=content_hash,
        object_key=stored.object_key,
        byte_size=byte_size,
        media_type=HTML_MEDIA_TYPE,
        extractor_version=parsed.extractor_version,
        fragments=fragments,
        fragment_count=len(fragments),
        detection_count=len(parsed.detections),
        truncated=parsed.truncated,
        limitations=limitations,
    )


def _ingest_as_new_version(
    conn: psycopg.Connection,
    principal: Principal,
    store: LocalBlobStore,
    snapshot: HtmlSnapshot,
    parsed: ParsedDocument,
    spec: HtmlIngestSpec,
) -> HtmlIngestResult:
    """A second retrieval of a source that is already in the catalogue.

    Three outcomes, and they are genuinely different things:

    * the bytes are identical to the current version — nothing changed, so no
      version is created. Saying "version 2" for an unchanged page would make
      the version number a count of submissions rather than a count of changes;
    * the bytes differ — a new version row with the new hash, the URL and the
      retrieval instant. Version 1 is not touched, and the new version's text is
      not in ``kb.fragment`` because the schema has nowhere to put it. Both
      limitations are reported in the result;
    * the caller may not see the source at all — refused, with the same answer
      as for a source that does not exist.
    """
    # The store is not touched on this path: a version has nowhere to put its
    # bytes (see _commit_new_version), and writing unreferenced bytes is the
    # residue C10 documented rather than something this card adds to.
    del store
    source_id = spec.source_id
    if source_id is None:  # pragma: no cover - the caller dispatches on this
        raise AccessDenied("no such source")
    found = _existing_version(conn, principal, source_id)
    if found is None:
        raise AccessDenied("no such source")
    library_id, version_no, current_hash = found
    if library_id != spec.library_id:
        # RLS already limited what could be read; a source in another library
        # would be invisible, so this only fires for a caller who is told about
        # a source that exists. Refuse rather than move a page between libraries.
        raise AccessDenied("no such source")

    ingest_id, is_replay = _begin_attempt(conn, principal, spec)
    if is_replay:
        return _replay(conn, principal, ingest_id)

    if current_hash == snapshot.content_hash:
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(
                "UPDATE kb.ingest SET state = 'committed', source_id = %s, "
                "committed_at = now(), content_hash = %s, source_url = %s, retrieved_at = %s "
                "WHERE id = %s AND submitted_by = %s",
                (
                    source_id,
                    snapshot.content_hash,
                    snapshot.final_url,
                    snapshot.retrieved_at,
                    ingest_id,
                    principal.principal_id,
                ),
            )
        return HtmlIngestResult(
            source_id=source_id,
            status="unchanged",
            version_no=version_no,
            url=snapshot.final_url,
            retrieved_at=snapshot.retrieved_at,
            content_hash=current_hash,
            object_key=None,
            byte_size=0,
            media_type=HTML_MEDIA_TYPE,
            extractor_version=parsed.extractor_version,
            fragments=(),
            fragment_count=0,
            detection_count=0,
            truncated=False,
            limitations=(
                "the page is byte-identical to the current version; no new version was "
                "created and no fragment was rewritten",
            ),
        )

    new_version = _commit_new_version(conn, principal, spec, ingest_id, source_id, snapshot)
    return HtmlIngestResult(
        source_id=source_id,
        status="versioned",
        version_no=new_version,
        url=snapshot.final_url,
        retrieved_at=snapshot.retrieved_at,
        content_hash=snapshot.content_hash,
        object_key=None,
        byte_size=0,
        media_type=HTML_MEDIA_TYPE,
        extractor_version=parsed.extractor_version,
        fragments=(),
        fragment_count=0,
        detection_count=0,
        truncated=parsed.truncated,
        limitations=(_LIMITATION_FRAGMENTS, _LIMITATION_BYTES),
    )


def _replay(
    conn: psycopg.Connection,
    principal: Principal,
    ingest_id: UUID,
) -> HtmlIngestResult:
    """The answer for a repeated submission: the source the first one produced.

    No write, no second source, no second version. Only ever returned to the
    principal that made the first submission — the row is visible to its
    submitter alone.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT source_id, content_hash, source_url, retrieved_at FROM kb.ingest WHERE id = %s",
            (ingest_id,),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:  # pragma: no cover - committed implies a source
        raise RuntimeError("a committed attempt carries no source")
    if row[1] is None or row[2] is None or row[3] is None:
        # Every row this card commits carries a hash, a URL and an instant. A
        # committed row without them came from somewhere else, and answering it
        # with a made-up retrieval time would be exactly the fabrication the
        # contract forbids.
        raise RuntimeError("the committed attempt carries no snapshot provenance")
    return HtmlIngestResult(
        source_id=row[0],
        status="replayed",
        version_no=1,
        url=row[2],
        retrieved_at=row[3],
        content_hash=row[1],
        object_key=None,
        byte_size=0,
        media_type=HTML_MEDIA_TYPE,
        extractor_version="",
        fragments=(),
        fragment_count=0,
        detection_count=0,
        truncated=False,
    )


# ------------------------------------------------------------------- reading


@dataclass(frozen=True)
class SnapshotRecord:
    """What a reader may see about one snapshot, under their own identity."""

    source_id: UUID
    library_id: UUID
    url: str | None
    retrieved_at: dt.datetime | None
    media_type: str
    current_version: int
    current_hash: str | None
    versions: tuple[tuple[int, str], ...]
    object_key: str | None
    fragment_count: int
    extractor_version: str | None = None


def read_snapshot(
    conn: psycopg.Connection, principal: Principal, source_id: UUID
) -> SnapshotRecord | None:
    """The snapshot record, or None when the caller may not see it.

    None means "not visible to you", which is also what "does not exist" means
    here. There is no path, no signed URL and no direct handle: every read comes
    back through a check, which is what makes a later revoke effective.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT s.id, s.library_id, s.media_type, m.object_key, v.version_no, v.content_hash "
            "FROM kb.source s "
            "LEFT JOIN kb.object_manifest m ON m.source_id = s.id "
            "JOIN kb.source_version v ON v.source_id = s.id AND v.version_no = "
            "(SELECT max(v2.version_no) FROM kb.source_version v2 WHERE v2.source_id = s.id) "
            "WHERE s.id = %s",
            (source_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        head = (row[0], row[1], row[2], row[3], int(row[4]), row[5])
        cur.execute(
            "SELECT version_no, content_hash FROM kb.source_version "
            "WHERE source_id = %s ORDER BY version_no",
            (source_id,),
        )
        versions = tuple((int(v), h) for v, h in cur.fetchall())
        cur.execute("SELECT count(*) FROM kb.fragment WHERE source_id = %s", (source_id,))
        fragment_count = int(_one(cur)[0])
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT source_url, retrieved_at FROM kb.ingest WHERE source_id = %s "
            "ORDER BY created_at LIMIT 1",
            (source_id,),
        )
        provenance = cur.fetchone()
    return SnapshotRecord(
        source_id=head[0],
        library_id=head[1],
        url=provenance[0] if provenance else None,
        retrieved_at=provenance[1] if provenance else None,
        media_type=head[2],
        current_version=head[4],
        current_hash=head[5],
        versions=versions,
        object_key=head[3],
        fragment_count=fragment_count,
    )


__all__ = [
    "DEFAULT_BACKEND_KEY",
    "HTML_MEDIA_TYPE",
    "MAX_SNAPSHOT_BYTES",
    "HtmlIngestResult",
    "HtmlIngestSpec",
    "SnapshotRecord",
    "ingest_html_snapshot",
    "read_snapshot",
]
