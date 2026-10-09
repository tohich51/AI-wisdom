"""Domain vocabulary. UI and MCP both call through these, never around them."""

from kb.domain.tiers import DATA_TIERS, DataTier, UsageState, tier_successors

__all__ = ["DATA_TIERS", "DataTier", "UsageState", "tier_successors"]
