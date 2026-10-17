"""Synthetic checks for the pure embedding and ranking boundaries."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID

import pytest

from kb.research.similarity import (
    _rank_similarity,
    _read_embedding_key,
    _validate_embedding_response,
    concept_text_hash,
)
from kb.retrieval.embedding_profile import SELECTED_PROFILE


def test_concept_hash_and_profile_are_exact_and_immutable():
    assert concept_text_hash("A", "B") != concept_text_hash("A\n", "")
    assert concept_text_hash("A", "B") != concept_text_hash("A", "C")
    assert SELECTED_PROFILE.model == "qwen/qwen3-embedding-8b"
    assert SELECTED_PROFILE.dimension == 4096
    assert len(SELECTED_PROFILE.profile_hash) == 64


def test_secure_embedding_key_is_read_from_owner_home(monkeypatch):
    with TemporaryDirectory(prefix=".kb-similarity-key-", dir=Path.home()) as directory:
        key_path = Path(directory) / "embedding.key"
        key_path.write_text("synthetic-provider-key\n", encoding="ascii")
        key_path.chmod(0o600)
        monkeypatch.setenv("KB_CONCEPT_EMBEDDING_KEY_FILE", str(key_path))
        assert _read_embedding_key() == "synthetic-provider-key"


def test_embedding_response_accepts_selected_model():
    vector = [1.0] + [0.0] * 4095
    selected = _validate_embedding_response(
        {
            "model": SELECTED_PROFILE.model,
            "data": [{"index": 0, "embedding": vector}],
        },
        1,
    )
    assert selected[0][0] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "model_payload",
    [
        {},
        {"model": None},
        {"model": "qwen/wrong-model"},
    ],
    ids=["missing", "null", "wrong-model"],
)
def test_embedding_response_rejects_missing_null_or_wrong_model(model_payload):
    vector = [1.0] + [0.0] * 4095
    payload = {
        **model_payload,
        "data": [{"index": 0, "embedding": vector}],
    }
    with pytest.raises(ValueError, match="embedding_provider_model_mismatch"):
        _validate_embedding_response(payload, 1)


def test_embedding_response_accepts_exact_upstream_model():
    vector = [1.0] + [0.0] * 4095
    upstream = _validate_embedding_response(
        {
            "model": "Qwen/Qwen3-Embedding-8B",
            "data": [{"index": 0, "embedding": vector}],
        },
        1,
    )
    assert upstream[0][0] == pytest.approx(1.0)


@pytest.mark.parametrize("component", [None, [[0.0]]])
def test_embedding_response_rejects_null_and_multidimensional_components(component):
    vector = [0.0] * 4096
    vector[0] = component
    with pytest.raises(ValueError, match="malformed_embedding_value"):
        _validate_embedding_response(
            {
                "model": SELECTED_PROFILE.model,
                "data": [{"index": 0, "embedding": vector}],
            },
            1,
        )


def test_embedding_response_rejects_zero_vector():
    with pytest.raises(ValueError, match="zero_embedding_vector"):
        _validate_embedding_response(
            {
                "model": SELECTED_PROFILE.model,
                "data": [{"index": 0, "embedding": [0.0] * 4096}],
            },
            1,
        )


@pytest.mark.parametrize(
    "component",
    [float("nan"), float("inf"), -float("inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_embedding_response_rejects_nonfinite_values(component):
    vector = [0.0] * 4096
    vector[0] = component
    with pytest.raises(ValueError, match="malformed_embedding_value"):
        _validate_embedding_response(
            {
                "model": SELECTED_PROFILE.model,
                "data": [{"index": 0, "embedding": vector}],
            },
            1,
        )


def test_similarity_ranking_is_undirected_ordered_and_deterministic():
    ids = [UUID(f"00000000-0000-0000-0000-00000000000{index}") for index in range(4)]
    vector = [1.0] + [0.0] * 4095
    concepts = [
        {"id": concept_id, "text_hash": f"{index:064x}"} for index, concept_id in enumerate(ids)
    ]
    edges = _rank_similarity(concepts, {item["text_hash"]: vector for item in concepts}, "a" * 64)
    assert [(str(edge["source"]), str(edge["target"])) for edge in edges] == [
        (str(ids[0]), str(ids[1])),
        (str(ids[0]), str(ids[2])),
        (str(ids[0]), str(ids[3])),
        (str(ids[1]), str(ids[2])),
        (str(ids[1]), str(ids[3])),
        (str(ids[2]), str(ids[3])),
    ]
    assert all(edge["score"] == pytest.approx(1.0) for edge in edges)
