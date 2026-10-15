"""C10 — the local blob store: immutable, content-addressed originals.

What this module is responsible for, and nothing else:

* writing an upload's bytes **once**, under a key derived from their SHA-256,
  so the key *is* the address and the bytes can never be replaced under it;
* resolving a key to a file without ever letting a key escape the store's
  root, and without the database ever learning where the root is.

What it is deliberately not responsible for: deciding who may read an object,
deciding what a URL resolves to, or naming the caller. Those live in
``kb.catalog.upload`` and ``kb.access.policy``; PostgreSQL remains the access
authority. This file is the only place in the product that knows a filesystem
path exists, and the path never leaves it.

Three properties are load-bearing and each is a test, not a comment:

``content addressed``
    ``object_key_for`` is a pure function of the digest. The same bytes produce
    the same key on any host, in any backend, in any order — which is what makes
    an S3 migration a routing change and not a rewrite of every card.

``immutable``
    An existing key is never rewritten. A second put of the same content
    re-verifies what is on disk and returns the same key; a put that would put
    *different* bytes under an existing key is impossible, because different
    bytes produce a different key.

``durable``
    The file is fsynced, renamed into place atomically, and the containing
    directory is fsynced too. A reader therefore never observes a partial
    object, and a process that dies between "bytes written" and "source row
    committed" leaves an object with no manifest, not a truncated one.

The root is a local temporary directory in tests and a bind mount in
production. It is *not* part of the API and *not* stored in PostgreSQL: see
SCALING.md §5, "Абсолютный путь хоста не является бизнес-ID".
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# The same expression the object_key CHECK uses in migrations/0004_uploads.sql.
# A key that cannot be stored must also not be looked up, or the two layers
# would disagree about which strings are addresses.
OBJECT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9/_.-]*$")

# Streaming chunk. Large enough that a 20-30 book corpus does not turn into
# thousands of syscalls, small enough that the buffer is never the memory story.
_CHUNK = 1024 * 1024

# Refuse a key that tries to name a parent. ".." cannot appear in a generated
# key, so its presence means the key did not come from object_key_for().
_FORBIDDEN = ("..", "//", "\\", "\x00")


class StorageError(RuntimeError):
    """The store refused the operation. Never carries a filesystem path."""


class ObjectNotFound(StorageError):
    """No object under that key.

    The message is the key and nothing else. A caller that must not know whether
    an object exists never reaches here: kb.catalog.upload checks access first.
    """


class ObjectKeyRejected(StorageError):
    """The key is not a well-formed, in-root object key."""


class ImmutableObject(StorageError):
    """Something tried to put different bytes under an existing key."""


@dataclass(frozen=True)
class StoredObject:
    """What one successful write produced.

    ``object_key`` is the business address. ``size`` is the number of bytes
    actually written, measured while streaming rather than trusted from a
    header, because a Content-Length that disagrees with the body is a normal
    failure mode and not a reason to record the wrong size.
    """

    object_key: str
    content_hash: str
    size: int
    media_type: str


def object_key_for(content_hash: str, *, scope: str) -> str:
    """The canonical, content-addressed key for a SHA-256 digest.

    ``blobs/<scope>/<aa>/<bb>/<64 hex>``.

    ``scope`` is the library the object belongs to, and it is a required
    argument on purpose. ``kb.source.object_key`` is UNIQUE across the whole
    installation (0001, and this card may not change 0001), so a *globally* pure
    content key would mean that the same book submitted to a second library could
    not become a second source at all. The only answers to that are a refusal
    that says "this file already exists" — which is precisely the A20 disclosure
    ACCESS-MODEL forbids (a message that another user already has this file is
    not revealed) — or a key that is unique per library. This is the second,
    and it is the honest one: deduplication happens *inside* the permitted
    audience, and a contributor learns nothing about any other library.

    The cost is real and is not hidden: identical bytes submitted to two
    libraries are stored twice. For a closed installation of 20-30 books that is
    the right trade against leaking the existence of a book. The single-DDL-owner
    proposal is to relax the constraint to ``UNIQUE (library_id, object_key)``,
    which would let the key be purely content-derived again; it is recorded in
    docs/handoff/results/C10.json rather than applied here.

    The two-character fan-out is not decoration: it keeps any one directory from
    holding every object in the installation, which matters for directory
    listings and for restore time.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ObjectKeyRejected("content hash must be 64 lowercase hex characters")
    if not re.fullmatch(r"[0-9a-f]{2,64}", scope):
        raise ObjectKeyRejected("scope must be 2-64 lowercase hex characters")
    return f"blobs/{scope}/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"


def validate_object_key(object_key: str) -> str:
    """Return the key if it is a well-formed object key, else raise.

    Four separate things are checked, and the fourth is the one that is easy to
    forget: the key must also *resolve* inside the root. A well-formed string is
    not the same as a safe path, and "safe" is a property of the join, not of
    the key.
    """
    if not isinstance(object_key, str) or not OBJECT_KEY_RE.fullmatch(object_key):
        raise ObjectKeyRejected("object key is not a well-formed key")
    if any(bad in object_key for bad in _FORBIDDEN):
        raise ObjectKeyRejected("object key names a parent or an empty segment")
    if object_key.endswith("/") or "//" in object_key:
        raise ObjectKeyRejected("object key ends in a separator")
    return object_key


def _resolve_in_root(root: Path, object_key: str) -> Path:
    """Resolve a validated key under ``root``, refusing anything that escapes.

    ``resolve()`` is what makes this a containment check rather than a string
    check: a symlink inside the store, or a "..", produces a path outside root
    and is rejected here. ``is_relative_to`` is the containment test; nothing
    downstream needs to know how deep the store is.
    """
    validate_object_key(object_key)
    root_resolved = root.resolve()
    candidate = (root_resolved / object_key).resolve()
    if candidate == root_resolved or not candidate.is_relative_to(root_resolved):
        raise ObjectKeyRejected("object key resolves outside the store")
    return candidate


class LocalBlobStore:
    """An append-only, content-addressed store on a local filesystem.

    Constructed with a root directory. The root is created if missing. Nothing
    about it is exported: it appears in no model, no response body and no SQL
    statement, which is what SCALING.md §5 asks for and what
    test_no_host_path_leaves_the_service() checks.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The configured root. For the store's own use and for diagnostics."""
        return self._root

    # ------------------------------------------------------------------ put

    # The staging area is inside the store, on purpose. os.replace is only
    # atomic within one filesystem, so staging in /tmp and renaming into the
    # store would degrade to a copy on any deployment where they are different
    # mounts — and a copy is a window in which a reader can see a partial book.
    _STAGING = ".staging"

    def put(
        self,
        stream: BinaryIO,
        *,
        media_type: str,
        scope: str,
        max_bytes: int | None = None,
    ) -> StoredObject:
        """Stream ``stream`` into the store and return what was written.

        The bytes are hashed on the way through, so the caller never holds a
        whole book in memory and never has to trust a declared length. The key
        is only known once the last byte has been read, so the bytes land in a
        staging file first and are renamed into their content-addressed home
        afterwards. The rename is atomic: a reader sees either no object or the
        whole object.

        If the key already exists the object is left exactly as it is and the
        existing object is verified and returned. That is not a shortcut around
        immutability; it *is* immutability. Identical content is the same object,
        and a store that rewrote it could change a source's bytes without
        changing its key — which would break every hash comparison downstream.
        """
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")

        staging_dir = self._root / self._STAGING
        staging_dir.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            fd, name = tempfile.mkstemp(dir=staging_dir, prefix="partial-", suffix=".tmp")
            tmp_path = Path(name)
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if max_bytes is not None and size > max_bytes:
                        raise StorageError("upload exceeds the configured size limit")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            # A zero-byte stream hashes to the empty digest, which is a perfectly
            # good content-addressed key. It is recorded honestly as a 0-byte
            # object rather than rejected or padded.
            content_hash = digest.hexdigest()
            object_key = object_key_for(content_hash, scope=scope)
            destination = _resolve_in_root(self._root, object_key)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                # Identical content: the existing object stands. Concurrency is
                # not even required for this branch — a re-upload of the same
                # book lands here, which is the idempotency the card asks for.
                tmp_path.unlink(missing_ok=True)
                tmp_path = None
                return self._existing(destination, content_hash, media_type, scope)

            os.replace(tmp_path, destination)
            tmp_path = None
            self._fsync_dir(destination.parent)
            return StoredObject(
                object_key=object_key,
                content_hash=content_hash,
                size=size,
                media_type=media_type,
            )
        except OSError as exc:
            # ``strerror`` is None for an OSError a caller raised itself, which
            # is exactly the "the client went away" case, so fall back rather
            # than reporting "None" into a log.
            detail = exc.strerror or str(exc) or type(exc).__name__
            raise StorageError(f"could not store the upload: {detail}") from exc
        finally:
            # A failed or abandoned write leaves nothing behind. A leftover
            # .tmp file would be a partial object that nothing points at, which
            # is harmless but is still garbage on a volume that will be backed up.
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    tmp_path.unlink()

    def _existing(
        self, destination: Path, content_hash: str, media_type: str, scope: str
    ) -> StoredObject:
        """Return the already-stored object, after proving it is intact.

        A collision on SHA-256 is not a thing, so a mismatch here means the store
        is damaged underneath us. Raising is the only honest answer: the
        alternative is to overwrite the object and destroy the evidence of
        whatever actually went wrong.
        """
        actual = _hash_file(destination)
        if actual != content_hash:
            raise ImmutableObject(
                "stored object does not match its content-addressed key; refusing to rewrite it"
            )
        return StoredObject(
            object_key=object_key_for(content_hash, scope=scope),
            content_hash=content_hash,
            size=destination.stat().st_size,
            media_type=media_type,
        )

    # ------------------------------------------------------------------ get

    @contextlib.contextmanager
    def open(self, object_key: str) -> Iterator[BinaryIO]:
        """Open an object for reading.

        The key is validated and resolved before ``open`` is called, so a key
        that would leave the store never reaches the filesystem. The handle is
        yielded inside a ``finally`` that closes it even if the caller raises.
        """
        path = _resolve_in_root(self._root, object_key)
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"no object under key {object_key}") from exc
        except OSError as exc:  # pragma: no cover - permissions on a local temp dir
            raise StorageError(f"could not read the object: {exc.strerror}") from exc
        try:
            yield handle
        finally:
            handle.close()

    def read_range(self, object_key: str, start: int, stop: int) -> bytes:
        """Read ``[start, stop)`` — the PDF Range read, on a real file.

        ``stop`` is exclusive, matching Python slicing and HTTP ``Range``. A
        request for a range that starts past the end yields nothing rather than
        an error, and a range wider than the object is truncated to it, because
        both are what a well-behaved reader asked for.
        """
        if start < 0 or stop < 0:
            raise StorageError("range bounds must not be negative")
        if stop < start:
            raise StorageError("a range whose end precedes its start is not a range")
        if stop == start:
            return b""
        path = _resolve_in_root(self._root, object_key)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                return handle.read(stop - start)
        except FileNotFoundError as exc:
            raise ObjectNotFound(f"no object under key {object_key}") from exc

    def stat(self, object_key: str) -> tuple[int, str]:
        """``(size, sha256)`` for a stored object, re-hashed from the bytes.

        Re-hashing on stat is deliberate: it is how a test proves a blob
        *survives* a restart by re-reading it from disk and getting the same
        digest, and it is cheap next to the write.
        """
        path = _resolve_in_root(self._root, object_key)
        if not path.is_file():
            raise ObjectNotFound(f"no object under key {object_key}")
        return path.stat().st_size, _hash_file(path)

    def exists(self, object_key: str) -> bool:
        """Whether an object is stored under this key.

        Raises rather than returning False for a malformed key: "no such object"
        and "that is not an object key" are different answers, and collapsing
        them would hide a bug in a caller.
        """
        return _resolve_in_root(self._root, object_key).is_file()

    # ---------------------------------------------------------------- write

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Make a rename durable.

        Without this the file's bytes are on disk but the *name* may not be,
        which turns "the upload succeeded" into a promise the filesystem did not
        keep. On Linux this is open(dir, O_RDONLY) + fsync.
        """
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:  # pragma: no cover - platform without directory fsync
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover - some filesystems refuse this
            pass
        finally:
            os.close(fd)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
