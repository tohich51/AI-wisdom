"""Smoke tests: the package imports and the tier model is encoded, not implied."""

from __future__ import annotations

import kb


def test_package_imports():
    assert kb.__version__ == "0.1.0"


def test_tier_order_is_declared():
    # The four tiers are the spine of PRODUCT-SPEC. A refactor that drops one
    # must fail here rather than silently shipping a three-tier product.
    from kb.domain.tiers import DATA_TIERS, tier_successors

    assert list(DATA_TIERS) == ["source", "knowledge", "rule", "experience"]
    assert tier_successors("source") == ["knowledge"]
    assert tier_successors("rule") == ["experience"]


def test_retrieval_is_not_application():
    """Served != applied != used. The distinction is the product's core claim."""
    from kb.domain.tiers import UsageState

    assert UsageState.RETRIEVED.value != UsageState.APPLIED.value
    assert UsageState.APPLIED.value != UsageState.OUTCOMED.value
