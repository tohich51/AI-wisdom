import os
import re
import stat
import tempfile
from pathlib import Path

from kb.retrieval.provisioning import SecretRef, secret_ref_for

_IDENTITY = re.compile(r"kb-svc-(read|index)-[0-9a-f]{32}")
_READ_IDENTITY = re.compile(r"kb-svc-read-[0-9a-f]{32}")


def _validate_identity(identity: str, *, read_only: bool = False) -> str:
    pattern = _READ_IDENTITY if read_only else _IDENTITY
    if not isinstance(identity, str) or pattern.fullmatch(identity) is None:
        raise ValueError("invalid service identity")
    return identity


def _reject_symlinks(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            candidate_stat = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            raise OSError("unable to inspect secret path") from None
        if stat.S_ISLNK(candidate_stat.st_mode):
            raise ValueError("symlinks are not permitted")


def _credential_path(root: Path, identity: str) -> Path:
    role = identity.split("-")[2]
    return root / role / identity / "key"


class FileSecretSink:
    def __init__(self, root: Path):
        self._root = root

    def write(self, identity: str, secret: str) -> SecretRef:
        identity = _validate_identity(identity)
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("secret must be non-empty")
        role_dir = self._root / identity.split("-")[2]
        identity_dir = role_dir / identity
        key_path = _credential_path(self._root, identity)
        _reject_symlinks(key_path)
        for directory in (self._root, role_dir, identity_dir):
            directory.mkdir(mode=0o700, exist_ok=True)
            _reject_symlinks(directory)
            os.chmod(directory, 0o700)
        descriptor, temporary_name = tempfile.mkstemp(dir=identity_dir)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(secret)
            _reject_symlinks(key_path)
            os.replace(temporary_path, key_path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
        return secret_ref_for(identity)

    def recover(self, identity: str) -> SecretRef | None:
        identity = _validate_identity(identity)
        key_path = _credential_path(self._root, identity)
        _reject_symlinks(key_path)

        try:
            path_stat = os.lstat(key_path)
        except FileNotFoundError:
            return None
        except OSError:
            raise OSError("unable to inspect secret file") from None

        if stat.S_ISLNK(path_stat.st_mode):
            raise ValueError("symlinks are not permitted")
        if not stat.S_ISREG(path_stat.st_mode):
            raise ValueError("secret key is not a regular file")
        if stat.S_IMODE(path_stat.st_mode) & 0o077:
            raise PermissionError("secret key permissions are too broad")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = os.open(key_path, flags)
        except FileNotFoundError:
            raise OSError("secret file changed during recovery") from None
        except OSError:
            raise OSError("unable to open secret file") from None

        try:
            try:
                opened_stat = os.fstat(descriptor)
            except OSError:
                raise OSError("unable to inspect opened secret file") from None
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ValueError("secret key is not a regular file")
            if stat.S_IMODE(opened_stat.st_mode) & 0o077:
                raise PermissionError("secret key permissions are too broad")
            if (opened_stat.st_dev, opened_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise OSError("secret file changed during recovery")

            nonempty = False
            try:
                while True:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    if chunk.strip():
                        nonempty = True
                        break
            except OSError:
                raise OSError("unable to read secret file") from None
            if not nonempty:
                raise ValueError("secret key is empty")

            _reject_symlinks(key_path)
            try:
                final_stat = os.lstat(key_path)
            except FileNotFoundError:
                raise OSError("secret file changed during recovery") from None
            except OSError:
                raise OSError("unable to inspect secret file") from None
            if (
                final_stat.st_dev,
                final_stat.st_ino,
                final_stat.st_mode,
                final_stat.st_size,
                final_stat.st_mtime_ns,
            ) != (
                path_stat.st_dev,
                path_stat.st_ino,
                path_stat.st_mode,
                path_stat.st_size,
                path_stat.st_mtime_ns,
            ):
                raise OSError("secret file changed during recovery")
        finally:
            try:
                os.close(descriptor)
            except OSError:
                raise OSError("unable to close secret file") from None

        return secret_ref_for(identity)


def read_credentials(root: Path, identity: str) -> str:
    identity = _validate_identity(identity, read_only=True)
    key_path = root / identity / "key"
    _reject_symlinks(key_path)
    if not key_path.is_file():
        raise FileNotFoundError("secret key is not a regular file")
    if key_path.stat().st_mode & 0o077:
        raise PermissionError("secret key permissions are too broad")
    with key_path.open("r", encoding="utf-8") as stream:
        secret = stream.read().strip()
    if not secret:
        raise ValueError("secret key is empty")
    return secret
