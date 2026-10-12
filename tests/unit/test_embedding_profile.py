import hashlib
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from kb.retrieval.embedding_profile import SELECTED_PROFILE


def test_profile_is_frozen_and_config_is_fresh():
    with pytest.raises(FrozenInstanceError):
        SELECTED_PROFILE.dimension = 1

    config = SELECTED_PROFILE.config()
    config["dimension"] = 1
    assert SELECTED_PROFILE.dimension == 4096
    assert SELECTED_PROFILE.config() == config | {"dimension": 4096}


def test_profile_hash_is_stable_and_canonical():
    payload = json.dumps(
        SELECTED_PROFILE.config(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected = hashlib.sha256(payload).hexdigest()

    assert SELECTED_PROFILE.profile_hash == expected
    assert SELECTED_PROFILE.profile_hash == SELECTED_PROFILE.profile_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dimension", 2048),
        ("query_instruction", "Use a different instruction"),
        ("chunking_policy", "published-knowledge-markdown:v2"),
    ],
)
def test_profile_hash_changes_with_semantic_configuration(field, value):
    changed = replace(SELECTED_PROFILE, **{field: value})
    assert changed.profile_hash != SELECTED_PROFILE.profile_hash


def test_query_text_adds_instruction_and_trims_query():
    assert SELECTED_PROFILE.query_text("  embedding models  ") == (
        "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
        "Query: embedding models"
    )


@pytest.mark.parametrize("text", ["", " \n\t "])
def test_query_text_rejects_blank_text(text):
    with pytest.raises(ValueError):
        SELECTED_PROFILE.query_text(text)


def test_query_text_rejects_oversize_text():
    with pytest.raises(ValueError):
        SELECTED_PROFILE.query_text("x" * 2001)


def test_query_text_rejects_non_string_text():
    with pytest.raises(TypeError):
        SELECTED_PROFILE.query_text(None)
