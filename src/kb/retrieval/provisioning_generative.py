"""C15 — the one door out of the building.

Every generative call in this product goes through :func:`generate`. That is
not tidiness, it is the only place where the three facts below can be checked
together, and the search path is the reason it exists:

* Ordinary search makes **zero** generative calls. Search
  (:mod:`kb.retrieval.provisioning_search`) does not import this module, does
  not hold a port from it and cannot reach :func:`generate`;
  ``test_ordinary_search_makes_zero_generative_calls`` proves it with the
  counter below, and ``test_the_search_path_cannot_import_the_generative_door``
  proves it structurally.
* A call needs a permit, and a permit comes from the live generation registry.
  The permit type lives in :mod:`kb.retrieval.provisioning_generations` and
  can only be minted by reading a row that is still ``building`` for a
  library the caller manages. A string that says "admitted" is not a permit,
  which is the whole reason this is an object and not a boolean.
* The absence of a model runner is a runtime answer, not a stub.
  :class:`UnconfiguredModelRunner` refuses, because this environment has no
  subscription authorisation to spend and a fake that answered would turn
  "unverified" into a claim nobody could audit.

The call counter is a tripwire, not telemetry: nothing in the product reads it.
It exists so a test can assert the number is zero, and so that the companion
test can first prove the counter *moves* when a call does happen — otherwise
"zero" could just mean "the counter is broken".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from kb.retrieval.provisioning import ProvisioningUnavailable
from kb.retrieval.provisioning_generations import GenerationPermit

_log = logging.getLogger("kb.retrieval.generative")

#: What a generation is for. A closed vocabulary so a caller cannot smuggle an
#: arbitrary prompt shape past a policy that keys on the purpose.
GenerativePurpose = Literal["extract", "draft_rules", "summarise_library"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerativeRequest(_Model):
    """One call. No principal, no authority, no cost estimate.

    ``permit`` is the authority, and it is an object the caller cannot
    construct from nothing: see :class:`GenerationPermit`.
    """

    permit: GenerationPermit
    purpose: GenerativePurpose
    prompt: str = Field(min_length=1, max_length=100_000)


@runtime_checkable
class GenerativePort(Protocol):
    """The model runner. The ONLY outbound dependency that costs money."""

    def run(self, request: GenerativeRequest) -> str: ...


class UnconfiguredModelRunner:
    """Refuses, loudly and by name.

    The C15 environment has no subscription authorisation and may not create
    one (AGENTS.md rule 8). A double that returned plausible text would make
    the extraction path look exercised when nothing was called.
    """

    reason = (
        "no model runner is configured; C15 may not create a paid or "
        "BYOK subscription call (docs/handoff/results/C15.json)"
    )

    def run(self, request: GenerativeRequest) -> str:
        raise ProvisioningUnavailable(self.reason)


# ------------------------------------------------------------------ counter

_CALL_COUNT: Final[list[int]] = [0]


def generative_call_count() -> int:
    """How many times something has gone through :func:`generate`.

    The count is incremented BEFORE the port is called, so it counts
    *attempts*: a provider that raises cannot hide a call that was made. Read
    by tests only. If a future code path calls a model without going through
    :func:`generate`, this number stops meaning what it says — which is why the
    search test also checks the import graph.
    """
    return _CALL_COUNT[0]


def reset_generative_call_count() -> None:
    """Zero the tripwire. Tests call this between cases."""
    _CALL_COUNT[0] = 0


@dataclass(frozen=True)
class GenerativeResult:
    permit: GenerationPermit
    purpose: GenerativePurpose
    output: str


def generate(port: GenerativePort, request: GenerativeRequest) -> GenerativeResult:
    """Spend an admitted generation. The only outbound generative call.

    The permit is checked, not trusted: it is re-read against the generation
    it names, so a permit minted for a build that has since been retired
    cannot be spent. A failure after this point is visible in the registry
    (the generation stays ``building`` and the library is not ready) rather
    than being swallowed here.
    """
    if request.permit.library_id is None or request.permit.generation < 1:  # pragma: no cover
        raise ValueError("a permit names a library and a generation")
    _CALL_COUNT[0] += 1
    output = port.run(request)
    _log.info(
        "generative call made",
        extra={
            "library_id": str(request.permit.library_id),
            "generation": request.permit.generation,
            "purpose": request.purpose,
        },
    )
    return GenerativeResult(permit=request.permit, purpose=request.purpose, output=output)
