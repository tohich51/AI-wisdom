"""Focused spy coverage for batched citation validation."""

from uuid import UUID, uuid4

import pytest

from kb.access.policy import AccessDenied
from kb.research import grounding


def _resolution(fragment_id: UUID):
    text = "alpha beta gamma delta"
    row = {
        "id": fragment_id,
        "text": text,
        "title": "Title",
        "author": "Author",
        "edition": None,
        "publication_ref": None,
    }
    pin = {
        "source_id": str(uuid4()),
        "text_hash": "a" * 64,
        "excerpt_start": 0,
        "excerpt_end": len(text),
    }
    source = {
        "source_version_id": str(uuid4()),
        "content_hash": "b" * 64,
    }
    citation = grounding._citation(row, pin, source, 6, 10)
    return citation, (row, pin, source)


def _install_canonical_spy(monkeypatch, resolutions, events):
    def unchecked(_cur, _manifest, fragment_id):
        events.append(("canonical", fragment_id))
        return resolutions[fragment_id]

    monkeypatch.setattr(grounding, "_canonical_unchecked", unchecked)


def test_single_and_batch_checks_keep_snapshot_boundaries(monkeypatch):
    first, first_resolution = _resolution(uuid4())
    second, second_resolution = _resolution(uuid4())
    resolutions = {
        UUID(first["fragment_id"]): first_resolution,
        UUID(second["fragment_id"]): second_resolution,
    }
    manifest = {"sources": [], "fragments": []}
    events = []
    _install_canonical_spy(monkeypatch, resolutions, events)

    def record_snapshot(_cur, checked_manifest):
        events.append(("snapshot", checked_manifest))

    monkeypatch.setattr(grounding, "check_snapshot", record_snapshot)

    assert grounding.validate_citation(object(), manifest, first) == first
    assert events == [
        ("snapshot", manifest),
        ("canonical", UUID(first["fragment_id"])),
    ]

    events.clear()
    assert grounding.validate_citations(object(), manifest, [first, second]) == [first, second]
    assert events == [
        ("canonical", UUID(first["fragment_id"])),
        ("canonical", UUID(second["fragment_id"])),
        ("snapshot", manifest),
    ]


def test_batch_rejects_forged_hash_for_each_citation(monkeypatch):
    citation, resolution = _resolution(uuid4())
    resolutions = {UUID(citation["fragment_id"]): resolution}
    events = []
    _install_canonical_spy(monkeypatch, resolutions, events)
    monkeypatch.setattr(grounding, "check_snapshot", lambda *_args: None)

    with pytest.raises(ValueError):
        grounding.validate_citations(object(), {}, [{**citation, "text_hash": "c" * 64}])

    assert events == [("canonical", UUID(citation["fragment_id"]))]


@pytest.mark.parametrize("citation_count", [0, 2])
def test_snapshot_failure_at_end_prevents_result(monkeypatch, citation_count):
    first, first_resolution = _resolution(uuid4())
    second, second_resolution = _resolution(uuid4())
    resolutions = {
        UUID(first["fragment_id"]): first_resolution,
        UUID(second["fragment_id"]): second_resolution,
    }
    citations = [first, second][:citation_count]
    manifest = {"sources": [], "fragments": []}
    events = []
    _install_canonical_spy(monkeypatch, resolutions, events)

    def deny_snapshot(_cur, checked_manifest):
        events.append(("snapshot", checked_manifest))
        raise AccessDenied("forbidden")

    monkeypatch.setattr(grounding, "check_snapshot", deny_snapshot)

    with pytest.raises(AccessDenied):
        grounding.validate_citations(object(), manifest, citations)

    expected = [("canonical", UUID(c["fragment_id"])) for c in citations]
    assert events == [*expected, ("snapshot", manifest)]
