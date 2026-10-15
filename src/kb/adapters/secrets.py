import os
import re
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
        if os.path.islink(candidate):
            raise ValueError("symlinks are not permitted")


class FileSecretSink:
    def __init__(self, root: Path):
        self._root = root

    def write(self, identity: str, secret: str) -> SecretRef:
        identity = _validate_identity(identity)
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError("secret must be non-empty")
        role = identity.split("-")[2]
        role_dir = self._root / role
        identity_dir = role_dir / identity
        key_path = identity_dir / "key"
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
