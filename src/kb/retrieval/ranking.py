from collections.abc import Sequence


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int = 60
) -> list[tuple[str, float]]:
    """Fuse rankings by reciprocal rank, deduplicating each list first."""
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")

    scores: dict[str, float] = {}
    for ranking in rankings:
        unique_ids = dict.fromkeys(ranking)
        for rank, doc_id in enumerate(unique_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))
