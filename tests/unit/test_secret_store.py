import pytest

from kb.adapters.secrets import FileSecretSink, read_credentials

READ = "kb-svc-read-" + "0" * 32
INDEX = "kb-svc-index-" + "1" * 32


def test_read_and_index_keys_use_separate_subdirectories(tmp_path):
    sink = FileSecretSink(tmp_path)
    sink.write(READ, "synthetic-read-value")
    sink.write(INDEX, "synthetic-index-value")
    read_key = tmp_path / "read" / READ / "key"
    index_key = tmp_path / "index" / INDEX / "key"
    assert read_key.read_text(encoding="utf-8") == "synthetic-read-value"
    assert index_key.read_text(encoding="utf-8") == "synthetic-index-value"
    assert not (tmp_path / READ).exists()
    assert not (tmp_path / INDEX).exists()


def test_key_files_and_directories_are_private(tmp_path):
    sink = FileSecretSink(tmp_path)
    for identity, role in ((READ, "read"), (INDEX, "index")):
        sink.write(identity, "synthetic-permission-value")
        key = tmp_path / role / identity / "key"
        assert (key.stat().st_mode & 0o777) == 0o600
        for directory in (tmp_path, tmp_path / role, key.parent):
            assert (directory.stat().st_mode & 0o777) == 0o700


def test_read_credentials_requires_read_subdirectory_and_rejects_index(tmp_path):
    sink = FileSecretSink(tmp_path)
    sink.write(READ, "synthetic-read-value")
    sink.write(INDEX, "synthetic-index-value")
    assert read_credentials(tmp_path / "read", READ) == "synthetic-read-value"
    with pytest.raises(FileNotFoundError):
        read_credentials(tmp_path, READ)
    with pytest.raises(ValueError, match="invalid service identity"):
        read_credentials(tmp_path / "read", INDEX)


def test_invalid_identities_and_path_traversal_are_rejected(tmp_path):
    sink = FileSecretSink(tmp_path)
    bad_identities = ("../escape", READ + "/../" + INDEX, "kb-svc-read-" + "0" * 31, 123)
    for identity in bad_identities:
        with pytest.raises(ValueError):
            sink.write(identity, "synthetic-value")
        with pytest.raises(ValueError):
            read_credentials(tmp_path / "read", identity)
    assert not any(tmp_path.iterdir())


def test_key_rotation_atomically_replaces_the_old_value(tmp_path):
    sink = FileSecretSink(tmp_path)
    sink.write(READ, "synthetic-old-value")
    key = tmp_path / "read" / READ / "key"
    before = key.stat()
    sink.write(READ, "synthetic-new-value")
    after = key.stat()
    assert read_credentials(tmp_path / "read", READ) == "synthetic-new-value"
    assert (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)


def test_existing_symlink_key_is_rejected_without_changing_target(tmp_path):
    target = tmp_path / "synthetic-target"
    target.write_text("unchanged-target-value", encoding="utf-8")
    key = tmp_path / "read" / READ / "key"
    key.parent.mkdir(parents=True)
    key.symlink_to(target)
    with pytest.raises(ValueError, match="symlinks are not permitted"):
        FileSecretSink(tmp_path).write(READ, "replacement-value")
    with pytest.raises(ValueError, match="symlinks are not permitted"):
        read_credentials(tmp_path / "read", READ)
    assert target.read_text(encoding="utf-8") == "unchanged-target-value"


def test_too_broad_key_permissions_are_rejected(tmp_path):
    sink = FileSecretSink(tmp_path)
    sink.write(READ, "synthetic-read-value")
    key = tmp_path / "read" / READ / "key"
    key.chmod(0o644)
    with pytest.raises(PermissionError, match="permissions are too broad"):
        read_credentials(tmp_path / "read", READ)


def test_empty_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="secret must be non-empty"):
        FileSecretSink(tmp_path).write(READ, "")
    FileSecretSink(tmp_path).write(READ, "synthetic-temporary-value")
    key = tmp_path / "read" / READ / "key"
    key.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="secret key is empty"):
        read_credentials(tmp_path / "read", READ)
