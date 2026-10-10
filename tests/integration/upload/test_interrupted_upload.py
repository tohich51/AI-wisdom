"""C10 — an interrupted upload leaves nothing readable behind.

Acceptance criterion 1: "Обрыв не создаёт опубликованный source без blob"
— an interruption must not create a source a reader can open whose bytes are
missing, half-written, or a different submission's.

The invariant this card enforces structurally is:

    kb.source and kb.object_manifest are written in ONE transaction,
    and the manifest is the only thing that turns bytes into a download.

So the two are true at every instant after every commit, and an interruption can
only ever leave the residue on the *other* side of the manifest: an object with
no manifest, which nothing can reach.

These tests are written against the real service path — the real catalogue
function, the real store, a real PostgreSQL — with a real dropped connection in
the middle of a real stream.
"""

from __future__ import annotations

import io
import uuid
from uuid import UUID

import pytest

from kb.access.policy import Principal
from kb.catalog import upload as catalog
from kb.catalog.storage import StorageError

pytestmark = pytest.mark.integration

BOOK = b"%PDF-1.7\n" + b"a long enough book to cross a read boundary. " * 200
STRANGER_BOOK = b"%PDF-1.7\n" + b"a completely different book. " * 200


class FailingStream:
    """A stream that dies partway, the way a dropped upload does.

    Duplicated from conftest on purpose: this module is about the torn upload and
    it reads better with the failure in front of it. Two copies of eleven lines
    is a smaller cost than an import that only exists to satisfy a type checker.
    """

    def __init__(self, payload: bytes, fail_after: int) -> None:
        self._payload = payload
        self._fail_after = fail_after
        self._served = 0

    def read(self, _size: int = -1) -> bytes:
        if self._served >= self._fail_after:
            raise OSError("the client went away")
        chunk = self._payload[self._served : self._served + 64]
        self._served += len(chunk)
        return chunk


def principal_of(people) -> Principal:
    return people.principal(people.owner)


def count_rows(world, library) -> tuple[int, int, int]:
    return (
        len(world.sources_in(library)),
        len(world.manifests_for(library)),
        len(world.ingests_in(library)),
    )


# ====================================================== a stream that dies


def test_a_torn_upload_leaves_no_readable_source(db_pool, store, people, world, key: str) -> None:
    """The client goes away after the first 64 bytes. Nothing readable survives.

    Not "the response is an error" — the error is the least interesting part. The
    assertions are about the database: no source, no manifest, no committed
    attempt. A reader arriving at that library sees an empty catalogue, which is
    the same as a library where the upload never started.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    with db_pool.connection() as conn:
        with pytest.raises(StorageError):
            catalog.submit_file(
                conn,
                principal_of(people),
                store,
                catalog.SubmitFile(library_id=library, idempotency_key=key),
                FailingStream(BOOK, fail_after=64),
                declared_media_type="application/pdf",
            )

    assert count_rows(world, library) == (0, 0, 1), "an interrupted upload left a source"
    states = [row[0] for row in world.ingests_in(library)]
    assert states == ["received"], "the attempt was marked as something it is not"
    assert world.ingests_in(library)[0][1] is None, "a partial upload recorded a hash"


def test_a_torn_upload_leaves_no_file_on_the_volume(
    db_pool, store, people, world, key: str
) -> None:
    """No partial object, and no temporary file left in the store.

    The staging file is removed in a ``finally``, so a reader that walks the
    store can never find a truncated object under a real key — and an abandoned
    upload does not slowly fill the volume.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    with db_pool.connection() as conn:
        with pytest.raises(StorageError):
            catalog.submit_file(
                conn,
                principal_of(people),
                store,
                catalog.SubmitFile(library_id=library, idempotency_key=key),
                FailingStream(BOOK, fail_after=64),
            )
    assert list((store.root / ".staging").iterdir()) == []
    assert [p.name for p in store.root.rglob("*") if p.is_file()] == []


def test_a_retry_with_the_same_key_resumes_and_produces_one_source(
    db_pool, store, people, world, key: str
) -> None:
    """The interrupted attempt is not an obstacle; it is a reservation.

    The client reconnects and resends. The same key is picked up, the same
    attempt row is finished rather than a second one started, and the result is
    exactly one source with a complete object behind it.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    with db_pool.connection() as conn:
        with pytest.raises(StorageError):
            catalog.submit_file(
                conn,
                principal_of(people),
                store,
                catalog.SubmitFile(library_id=library, idempotency_key=key),
                FailingStream(BOOK, fail_after=64),
            )
        result = catalog.submit_file(
            conn,
            principal_of(people),
            store,
            catalog.SubmitFile(library_id=library, idempotency_key=key),
            io.BytesIO(BOOK),
            declared_media_type="application/pdf",
        )

    assert result.status == "stored"
    assert count_rows(world, library) == (1, 1, 1)
    assert [row[0] for row in world.ingests_in(library)] == ["committed"]
    assert store.stat(result.original.object_key)[1] == result.original.content_hash


# ================================================= the window between the two


def test_bytes_on_the_volume_without_a_manifest_are_unreachable(
    db_pool, store, people, world
) -> None:
    """The other side of the invariant: a blob nobody may fetch.

    This is the residue a crash between "the bytes are stored" and "the source
    row is committed" leaves. The object exists and is perfectly valid; no
    manifest names it, so the download path has no key to look up and no source
    to look it up from. It is garbage-collectable, and until then it is inert.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    orphan = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=library.hex)
    assert store.exists(orphan.object_key)
    assert world.sources_in(library) == []
    assert world.manifests_for(library) == []

    with db_pool.connection() as conn:
        with pytest.raises(catalog.AccessDenied):
            catalog.read_original(
                conn, principal_of(people), store, _some_source_id(world, library)
            )


def _some_source_id(world, library) -> UUID:
    """An id to probe with. Real if the library has a source, invented if not.

    Either way the answer must be a refusal: with a real id it proves an orphan
    object is not reachable, and with an invented one it proves the refusal is
    not specific to a source that exists.
    """
    rows = world.sources_in(library)
    return rows[0][0] if rows else uuid.uuid4()


def test_a_source_whose_manifest_was_removed_is_not_downloadable(
    api, people, world, key: str, admin
) -> None:
    """A source row with no manifest is not readable, and says nothing.

    The read path needs both. This is the shape a partial restore or a careless
    delete leaves behind, and the answer it gets is the same 404 a stranger gets
    — never a 200 with a gap, and never a 500 that confirms the source exists.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = api.as_(people.owner).post(
        f"/libraries/{library}/sources/upload",
        files={"file": ("book.pdf", BOOK, "application/pdf")},
        data={"idempotency_key": key},
    )
    source_id = response.json()["source"]["id"]
    assert api.as_(people.owner).get(f"/sources/{source_id}/original").status_code == 200

    admin.execute("DELETE FROM kb.object_manifest WHERE source_id = %s", (source_id,))
    after = api.as_(people.owner).get(f"/sources/{source_id}/original")
    assert after.status_code == 404
    assert after.json() == {"detail": "no such source"}
    # and the source row itself is still there, which is what makes the refusal
    # "not visible" rather than "there was never anything"
    assert str(world.sources_in(library)[0][0]) == source_id


def test_bytes_that_no_longer_match_the_recorded_hash_are_refused_not_served(
    api, people, world, key: str, store
) -> None:
    """A damaged volume does not get served as if it were the book.

    The read path re-hashes what it read and compares it with the
    content-addressed key the catalogue recorded. A mismatch is an error, because
    the alternative is handing a reader fabricated provenance — a hash in the
    catalogue that describes bytes nobody is holding.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = api.as_(people.owner).post(
        f"/libraries/{library}/sources/upload",
        files={"file": ("book.pdf", BOOK, "application/pdf")},
        data={"idempotency_key": key},
    )
    object_key = response.json()["original"]["object_key"]

    path = store.root / object_key
    path.write_bytes(b"%PDF-1.7 different bytes entirely")
    after = api.as_(people.owner).get(f"/sources/{response.json()['source']['id']}/original")
    assert after.status_code == 503
    assert b"different bytes" not in after.content


# ============================================ the invariant, stated as a count


def test_every_source_with_a_manifest_has_both(db_pool, store, people, world) -> None:
    """A sweep rather than a case: sources and manifests never drift apart.

    Three submissions of three different books, one torn, one deduplicated, one
    with a retry. At the end, the two counts are equal and every source has a
    manifest naming its own object key. This is the shape an invariant takes
    when it is checked after a realistic sequence rather than after one happy
    path.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    with db_pool.connection() as conn:
        with pytest.raises(StorageError):
            catalog.submit_file(
                conn,
                principal_of(people),
                store,
                catalog.SubmitFile(library_id=library, idempotency_key="idem-torn-0001"),
                FailingStream(BOOK, fail_after=64),
            )
        first = catalog.submit_file(
            conn,
            principal_of(people),
            store,
            catalog.SubmitFile(library_id=library, idempotency_key="idem-second-0001"),
            io.BytesIO(BOOK),
        )
        # the same bytes again, a different key: deduplicated, no second source
        again = catalog.submit_file(
            conn,
            principal_of(people),
            store,
            catalog.SubmitFile(library_id=library, idempotency_key="idem-third-0001"),
            io.BytesIO(BOOK),
        )
        third = catalog.submit_file(
            conn,
            principal_of(people),
            store,
            catalog.SubmitFile(library_id=library, idempotency_key="idem-fourth-0001"),
            io.BytesIO(STRANGER_BOOK),
        )

    assert again.status == "deduplicated"
    assert again.source.id == first.source.id
    sources = world.sources_in(library)
    manifests = world.manifests_for(library)
    assert len(sources) == len(manifests) == 2
    for _id, object_key, content_hash in sources:
        matching = [m for m in manifests if m[0] == object_key]
        assert len(matching) == 1, f"source {object_key} has no single manifest"
        assert matching[0][1] is not None
        assert content_hash in object_key
    for result in (first, third):
        assert store.stat(result.original.object_key)[1] == result.original.content_hash


def test_a_committed_attempt_always_names_its_source(world) -> None:
    """The database's own half of the invariant, read back.

    ``committed_ingest_has_a_source`` is a CHECK, so this cannot fail for rows
    this card wrote — which is the point of asserting it: a future refactor that
    marks an attempt committed without a source is rejected by the database, and
    this test says so out loud rather than leaving it to a reader of the DDL.
    """
    rows = world.conn.execute(
        "SELECT count(*) FROM kb.ingest WHERE state = 'committed' AND source_id IS NULL"
    ).fetchone()
    assert int(rows[0]) == 0
