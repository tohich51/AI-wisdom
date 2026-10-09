"""Data tiers and usage states.

These two enums are the spine of the product. Everything else — schema, API,
MCP tools, UI — is a projection of what is written here, so that the admin UI
and the MCP endpoint cannot drift apart in their understanding of the model.

Invariants encoded below, taken from PRODUCT-SPEC:
  * retrieved means *served*. It does not mean applied, and it does not mean
    the outcome is known.
  * a rule carries an immutable version; experience points at that exact
    version, never at "the current rule".
  * a missing required attribute is null/unknown. It is never invented.
"""

from __future__ import annotations

from enum import StrEnum


class DataTier(StrEnum):
    SOURCE = "source"
    KNOWLEDGE = "knowledge"
    RULE = "rule"
    EXPERIENCE = "experience"


class UsageState(StrEnum):
    """Lifecycle of a knowledge item as seen by a consumer.

    The ordering is meaningful and monotonic: a served item does not
    retroactively become an applied one without an event.
    """

    RETRIEVED = "retrieved"
    APPLIED = "applied"
    OUTCOMED = "outcomed"
    CORRECTED = "corrected"


DATA_TIERS: tuple[str, ...] = tuple(t.value for t in DataTier)

_SUCCESSORS: dict[str, list[str]] = {
    DataTier.SOURCE.value: [DataTier.KNOWLEDGE.value],
    DataTier.KNOWLEDGE.value: [DataTier.RULE.value],
    DataTier.RULE.value: [DataTier.EXPERIENCE.value],
    DataTier.EXPERIENCE.value: [],
}


def tier_successors(tier: str) -> list[str]:
    """Downstream tiers that may legitimately be derived from `tier`."""
    try:
        return list(_SUCCESSORS[tier])
    except KeyError:
        raise ValueError(f"unknown tier {tier!r}; expected one of {DATA_TIERS}") from None


# Fields that must stay null rather than be guessed, per PRODUCT-SPEC.
NULLABLE_PROVENANCE_FIELDS: tuple[str, ...] = (
    "author",
    "publication",
    "retrieved_at",
    "locator",
)
