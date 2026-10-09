"""C10 — the object store: content-addressed, immutable, durable, and confined.

The card's core claim is that ``source.object_key`` is immutable and
content-addressed, and that re-uploading identical content is idempotent. These
tests are where that claim is checked, on a real filesystem under ``tmp_path``.

Nothing here is mocked. A "restart" is a second :class:`LocalBlobStore` pointed at
the same directory, which is the honest version of that word for a local volume:
the object survives because it was fsynced and renamed into place, not because
anything in memory remembered it.
"""

from __future__ import annotations

import hashlib
import io
import os

import pytest

from kb.catalog.storage import (
    ImmutableObject,
    LocalBlobStore,
    ObjectKeyRejected,
    ObjectNotFound,
    StorageError,
    object_key_for,
    validate_object_key,
)

pytestmark = pytest.mark.integration

BOOK = b"%PDF-1.7\n" + b"a page of a book. " * 400
BOOK_HASH = hashlib.sha256(BOOK).hexdigest()
# A stand-in for a library id's hex. The store is content-addressed within this
# scope; the catalogue is what decides the scope.
SCOPE = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


# ========================================================== the key itself


def test_the_key_is_a_pure_function_of_the_digest() -> None:
    """Content addressing means: same bytes, same key, on any host, any order."""
    assert object_key_for(BOOK_HASH, scope=SCOPE) == object_key_for(BOOK_HASH, scope=SCOPE)
    assert object_key_for(BOOK_HASH, scope=SCOPE).endswith(BOOK_HASH)
    # and it does not depend on anything about the caller
    assert object_key_for(hashlib.sha256(b"x").hexdigest(), scope=SCOPE) != object_key_for(
        BOOK_HASH, scope=SCOPE
    )


def test_the_key_fans_out_so_no_directory_holds_everything() -> None:
    key = object_key_for(BOOK_HASH, scope=SCOPE)
    parts = key.split("/")
    assert parts[0] == "blobs"
    assert parts[1] == SCOPE
    assert parts[2] == BOOK_HASH[:2]
    assert parts[3] == BOOK_HASH[2:4]


def test_a_key_must_match_the_stored_column_check() -> None:
    """The same expression as kb.object_manifest's CHECK.

    A key the database would refuse must not be accepted by the store, or the
    two layers would disagree about which strings are addresses.
    """
    assert validate_object_key(object_key_for(BOOK_HASH, scope=SCOPE))
    for bad in ("", "Blobs/x", "/blobs/x", "blobs//x", "blobs/x/", "blobs/../x", "a" * 5 + "!"):
        with pytest.raises(ObjectKeyRejected):
            validate_object_key(bad)


def test_a_hash_that_is_not_a_hash_is_refused() -> None:
    for bad in ("", "abc", BOOK_HASH.upper(), BOOK_HASH + "0", "z" * 64):
        with pytest.raises(ObjectKeyRejected):
            object_key_for(bad, scope=SCOPE)


def test_a_scope_cannot_escape_the_store() -> None:
    """The scope is a caller-supplied segment, so it is validated like one.

    ``scope`` comes from a library id in the product, but a store that trusted
    the segment would be one refactor away from a traversal, and the test is
    cheaper than the incident.
    """
    for bad in ("", "..", "../etc", "a/b", "/blobs", "Blobs", "abz", "0" * 65):
        with pytest.raises(ObjectKeyRejected):
            object_key_for(BOOK_HASH, scope=bad)


# ============================================================== writing


def test_an_upload_is_stored_under_its_digest(store: LocalBlobStore) -> None:
    stored = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    assert stored.object_key == object_key_for(BOOK_HASH, scope=SCOPE)
    assert stored.content_hash == BOOK_HASH
    assert stored.size == len(BOOK)
    with store.open(stored.object_key) as handle:
        assert handle.read() == BOOK


def test_re_uploading_identical_content_returns_the_same_object(store: LocalBlobStore) -> None:
    """Idempotency at the storage layer: one object, not two.

    The second put is not a no-op for the caller — it re-reads and re-verifies
    what is on disk — but it leaves exactly one file. A store that appended or
    rewrote would be able to change a source's bytes without changing its key.
    """
    first = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    second = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    assert first.object_key == second.object_key
    files = [
        os.path.join(root, name)
        for root, _dirs, names in os.walk(store.root)
        for name in names
        if not name.endswith(".tmp")
    ]
    assert len(files) == 1, files


def test_different_content_gets_a_different_object(store: LocalBlobStore) -> None:
    first = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    second = store.put(io.BytesIO(BOOK + b"!"), media_type="application/pdf", scope=SCOPE)
    assert first.object_key != second.object_key
    with store.open(first.object_key) as handle:
        assert handle.read() == BOOK, "the first object was rewritten"


def test_an_existing_object_is_never_overwritten(store: LocalBlobStore) -> None:
    """Immutability is enforced at the store, not merely assumed by the caller."""
    stored = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    # Reach past the content-addressed path and plant different bytes there, the
    # way a damaged volume or a careless migration would.
    path = store.root / stored.object_key
    path.write_bytes(b"corrupted")
    with pytest.raises(ImmutableObject):
        store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    # and the damage was not papered over
    assert path.read_bytes() == b"corrupted"


def test_a_zero_byte_upload_is_recorded_honestly(store: LocalBlobStore) -> None:
    """An empty file is a real file with a real digest, not a rejected request."""
    empty = hashlib.sha256(b"").hexdigest()
    stored = store.put(io.BytesIO(b""), media_type="text/plain", scope=SCOPE)
    assert stored.content_hash == empty
    assert stored.size == 0
    assert store.read_range(stored.object_key, 0, 10) == b""
    again = store.put(io.BytesIO(b""), media_type="text/plain", scope=SCOPE)
    assert again.object_key == stored.object_key


def test_an_upload_over_the_limit_is_refused_and_nothing_is_kept(store: LocalBlobStore) -> None:
    with pytest.raises(StorageError):
        store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE, max_bytes=10)
    leftovers = [n for n in os.listdir(store.root / ".staging")]
    assert leftovers == [], f"a partial upload was left behind: {leftovers}"


def test_a_failed_upload_leaves_no_partial_file(store: LocalBlobStore) -> None:
    class Broken(io.RawIOBase):
        def __init__(self) -> None:
            self._served = 0

        def read(self, _size: int = -1) -> bytes:
            self._served += 16
            if self._served > 64:
                raise OSError("the client went away")
            return b"z" * 16

    # A dropped connection surfaces as a StorageError, not as an OSError leaking
    # out of the store: the route turns it into a status code, and an unhandled
    # OSError would be a 500 with a filesystem path in the log.
    with pytest.raises(StorageError) as raised:
        store.put(Broken(), media_type="text/plain", scope=SCOPE)
    assert "client went away" in str(raised.value)
    assert list((store.root / ".staging").iterdir()) == []
    assert not any(name.endswith(".tmp") for _r, _d, files in os.walk(store.root) for name in files)


def test_a_large_object_is_streamed_not_buffered(store: LocalBlobStore) -> None:
    """A body bigger than the read chunk is written correctly.

    The chunk is 1 MiB, so this crosses it. It is a modest size on purpose: the
    property under test is that the loop handles more than one chunk, not that
    the store can take a 300 MB PDF.
    """
    payload = os.urandom(3 * 1024 * 1024)
    stored = store.put(io.BytesIO(payload), media_type="application/octet-stream", scope=SCOPE)
    assert stored.size == len(payload)
    assert store.stat(stored.object_key) == (len(payload), hashlib.sha256(payload).hexdigest())


# ============================================================ durability


def test_an_object_survives_a_restart(store: LocalBlobStore, tmp_path) -> None:
    """A second store over the same directory sees the same bytes and digest.

    This is the filesystem's version of a process restart, and it is the honest
    one: nothing is carried over in memory. What makes it work is that the file
    was fsynced, renamed into place, and the directory fsynced after the rename.
    """
    stored = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    del store

    reopened = LocalBlobStore(tmp_path / "objects")
    assert reopened.exists(stored.object_key)
    assert reopened.stat(stored.object_key) == (len(BOOK), BOOK_HASH)
    with reopened.open(stored.object_key) as handle:
        assert handle.read() == BOOK


def test_a_reader_never_sees_a_partial_object(store: LocalBlobStore) -> None:
    """Atomic publication: the key exists only once the whole object is there."""
    stored = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    # No .tmp file was promoted, and the promoted one is complete.
    assert store.stat(stored.object_key)[1] == BOOK_HASH
    assert [p.name for p in (store.root / ".staging").iterdir()] == []


# ============================================================== reading


def test_a_range_read_returns_exactly_the_requested_slice(store: LocalBlobStore) -> None:
    stored = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    assert store.read_range(stored.object_key, 0, 5) == BOOK[:5]
    assert store.read_range(stored.object_key, 5, 9) == BOOK[5:9]
    assert store.read_range(stored.object_key, 0, 10**9) == BOOK
    assert store.read_range(stored.object_key, len(BOOK), len(BOOK) + 10) == b""


def test_a_negative_or_inverted_range_is_refused(store: LocalBlobStore) -> None:
    stored = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    with pytest.raises(StorageError):
        store.read_range(stored.object_key, -1, 5)
    with pytest.raises(StorageError):
        store.read_range(stored.object_key, 9, 5)
    # an empty range is not an error, it is empty
    assert store.read_range(stored.object_key, 9, 9) == b""


def test_reading_an_absent_object_says_so(store: LocalBlobStore) -> None:
    missing = object_key_for("a" * 64, scope=SCOPE)
    with pytest.raises(ObjectNotFound):
        store.stat(missing)
    with pytest.raises(ObjectNotFound):
        store.read_range(missing, 0, 1)
    with pytest.raises(ObjectNotFound):
        with store.open(missing):
            pass


# ====================================================== path containment


@pytest.mark.parametrize(
    "key",
    [
        "../outside",
        "../../etc/passwd",
        "blobs/../../escape",
        "/etc/passwd",
        "..",
        "a/../../b",
    ],
)
def test_a_key_cannot_escape_the_store_root(store: LocalBlobStore, key: str) -> None:
    """ACCESS-MODEL A14, the path-traversal half.

    Every one of these is refused before ``open`` is called, and the check is on
    the *resolved* path, so a symlink inside the store is caught by the same
    code rather than by a second mechanism nobody remembers to write.
    """
    with pytest.raises(ObjectKeyRejected):
        store.stat(key)
    with pytest.raises(ObjectKeyRejected):
        store.read_range(key, 0, 10)
    assert not (store.root.parent / "outside").exists()


def test_a_symlink_inside_the_store_does_not_open_a_file_outside_it(
    store: LocalBlobStore, tmp_path
) -> None:
    """Containment is checked after resolution, so a planted symlink is caught."""
    secret = tmp_path / "secret.txt"
    secret.write_text("not for the object store")
    link = store.root / "blobs" / "link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    with pytest.raises(ObjectKeyRejected):
        store.stat("blobs/link")
    with pytest.raises(ObjectKeyRejected):
        with store.open("blobs/link"):
            pass


def test_the_store_root_is_never_part_of_an_object_key(store: LocalBlobStore) -> None:
    """SCALING.md §5: a host absolute path is not a business id.

    The key is a relative, content-derived address. The root is a property of the
    deployment and appears in no key, which is what makes the eventual move to a
    different backend or a different host a routing change.
    """
    stored = store.put(io.BytesIO(BOOK), media_type="application/pdf", scope=SCOPE)
    assert not stored.object_key.startswith("/")
    assert str(store.root) not in stored.object_key
    assert ".." not in stored.object_key
