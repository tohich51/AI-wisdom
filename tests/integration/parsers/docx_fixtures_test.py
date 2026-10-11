"""C12A — the fixtures are generated, and they are still the ones we shipped.

``evals/fixtures/docx`` is a directory of binaries in a source repository, and a
directory of binaries is where a real document ends up by accident: somebody
drops in a file from a customer to reproduce a bug, and it is now part of the
delivery bundle. Two tests here close that door.

The first regenerates every fixture from ``build_fixtures.py`` and compares
SHA-256. A fixture that was hand-edited after the fact fails the suite instead
of quietly becoming something the parser was never tested against.

The second reads the core properties of each committed package and requires them
to be the untouched python-docx template values. A real document carries an
author, a title, a revision history and often a company name in there; a
synthetic one carries ``python-docx`` and nothing else. It is a cheap check, and
it is the kind of check that is only cheap because it is there.
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib

import pytest
from docx import Document
from docx_fixtures import DOCX_FIXTURES

pytest_plugins = ["docx_fixtures"]
pytestmark = pytest.mark.integration

BUILDER = DOCX_FIXTURES / "build_fixtures.py"
MAX_FIXTURE_BYTES = 200_000


def _load_builder():
    spec = importlib.util.spec_from_file_location("c12a_build_fixtures", BUILDER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _committed() -> dict[str, pathlib.Path]:
    return {p.name: p for p in sorted(DOCX_FIXTURES.glob("*.docx"))}


def test_the_builder_and_the_committed_fixtures_agree(tmp_path):
    """Byte-for-byte. The build is reproducible, so this can be an equality."""
    assert BUILDER.exists(), "the builder is part of the fixtures, not optional"
    builder = _load_builder()
    expected = {p.name: p for p in builder.build_all(tmp_path / "rebuild")}
    committed = _committed()
    assert set(committed) == set(expected), (
        "a fixture is committed that the builder does not produce"
    )
    for name, rebuilt in expected.items():
        assert _sha256(rebuilt) == _sha256(committed[name]), name


def test_every_fixture_is_small_enough_to_read():
    for name, path in _committed().items():
        assert path.stat().st_size < MAX_FIXTURE_BYTES, f"{name} is {path.stat().st_size} bytes"


def test_no_fixture_carries_the_identity_of_a_real_document():
    for name, path in _committed().items():
        properties = Document(str(path)).core_properties
        assert properties.author == "python-docx", name
        assert properties.title == "", name
        assert properties.last_modified_by == "", name
        assert properties.subject == "", name
        assert properties.category == "", name
        assert properties.keywords == "", name
        assert properties.identifier == "", name


def test_the_fixtures_carry_no_external_relationship():
    """A fixture that reached out to the network or to a template on disk would
    be a dependency this card cannot account for.

    Relationship *Type* values are XML namespace URIs and are expected to
    contain ``http``; what must not appear is an external target, which is what
    ``TargetMode="External"`` marks.
    """
    import zipfile

    for name, path in _committed().items():
        with zipfile.ZipFile(path) as archive:
            members = archive.namelist()
            assert not [m for m in members if m.startswith("/") or ".." in m], name
            for member in members:
                if not member.endswith(".rels"):
                    continue
                rels = archive.read(member).decode("utf-8")
                assert 'TargetMode="External"' not in rels, f"{name}/{member}"
                for scheme in ("file://", "\\\\"):
                    assert scheme not in rels, f"{name}/{member} references {scheme}"


def test_the_fixtures_directory_holds_nothing_but_the_fixtures():
    """A stray binary in here is how a real document ends up in the bundle."""
    allowed = {".docx", ".md", ".py"}
    for entry in DOCX_FIXTURES.iterdir():
        if entry.is_dir() and entry.name == "__pycache__":
            continue  # written by this very suite when it imports the builder
        assert entry.suffix in allowed, f"unexpected file in the fixtures directory: {entry.name}"


def test_every_fixture_is_a_real_word_package_not_a_dict_shaped_like_one():
    """The parser's premise: a ``.docx`` is a ZIP with these parts in it."""
    import zipfile

    for name, path in _committed().items():
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        assert "[Content_Types].xml" in names, name
        assert "word/document.xml" in names, name
        assert "word/styles.xml" in names, name
