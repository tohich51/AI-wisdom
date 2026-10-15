import pytest

from kb.retrieval.ranking import reciprocal_rank_fusion


def test_multi_list_fusion_ordering() -> None:
    result = reciprocal_rank_fusion([["a", "b", "c"], ["b", "d"]])

    assert [doc_id for doc_id, _ in result] == ["b", "a", "d", "c"]
    assert [score for _, score in result] == pytest.approx(
        [1 / 62 + 1 / 61, 1 / 61, 1 / 62, 1 / 63]
    )


def test_duplicate_deweighting_and_rank() -> None:
    result = reciprocal_rank_fusion([["x", "b", "x", "a"]])

    assert [doc_id for doc_id, _ in result] == ["x", "b", "a"]
    assert [score for _, score in result] == pytest.approx([1 / 61, 1 / 62, 1 / 63])


def test_tie_is_deterministic() -> None:
    result = reciprocal_rank_fusion([["b", "a"], ["a", "b"]])

    assert [doc_id for doc_id, _ in result] == ["a", "b"]
    assert result[0][1] == pytest.approx(result[1][1])


@pytest.mark.parametrize("rankings", [[], [[]], [[], []]])
def test_empty(rankings: list[list[str]]) -> None:
    assert reciprocal_rank_fusion(rankings) == []


@pytest.mark.parametrize("k", [True, False, 0, -1, 1.5, "60", None])
def test_invalid_k(k: object) -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], k=k)  # type: ignore[arg-type]
