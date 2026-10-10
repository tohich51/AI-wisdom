"""C15 — ordinary search: routing, scope and the zero-generation invariant.

This module is the read side of the retrieval tier, and the property that
matters most about it is what it does **not** do:

    it never spends a generative call.

That is a product invariant, not an optimisation — "Нормальный поиск без
облачной генерации", vectors-only, with the generative planner/reranker/digest
of OpenViking left off. It is enforced three ways, because each way alone
could be defeated by an innocent-looking edit:

1. **The import graph.** This module does not import
   :mod:`kb.retrieval.provisioning_generative`, holds no
   :class:`~kb.retrieval.provisioning.IndexAdminPort`, and has no parameter
   through which a model runner could arrive.
   ``test_the_search_path_cannot_import_the_generative_door`` reads the AST.
2. **The request model.** :class:`SearchQuery` has three fields and
   ``extra="forbid"``: a caller cannot pass ``account_ref``, ``uri``,
   ``generate=true`` or a namespace, because the model does not accept them.
3. **The registry.** Even a caller who asked could not open a generation:
   admission is opt-in, owner-started and globally serialised in the database
   (see :mod:`kb.retrieval.provisioning_generations`).

What search *does* do here, and why each step is where it is:

* **Resolve accounts from PostgreSQL, never from the request.** The mapping is
  a server-side table under RLS. A client that asks about a library it cannot
  read gets no scope at all — the answer is "not in your libraries", not
  "that account exists".
* **Skip a library whose index is not ready, and say why.** A rebuild in
  flight, or content that has moved past the index, produces an explicit
  skip reason. ARCHITECTURE §8 asks for a visible ``partial`` rather than a
  false "nothing found", and this is where that is produced.
* **Re-check before handing anything out.** Every hit the index returns is
  re-resolved against PostgreSQL under the caller's identity before it is
  reported, so a hit that names a foreign library, a foreign account or a
  generation that is no longer current is dropped rather than displayed.
"""

from __future__ import annotations

import logging
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import Principal, transaction_identity
from kb.retrieval.provisioning import ProvisionedAccount, load_account
from kb.retrieval.provisioning_generations import IndexStatus, SkipReason, read_index_status

_log = logging.getLogger("kb.retrieval.search")

#: The retrieval profile. `vectors_only` is a property of the index, not a
#: request parameter: the value below is what the index was built with, and
#: the generation policy decides whether a model may be spent at all.
READ_MODE: Literal["vectors_only"] = "vectors_only"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchQuery(_Model):
    """What a caller may ask for.

    Three fields, and the absence is the design. There is no ``account_ref``:
    an account is a server-side mapping, and a caller that could name one
    would be able to aim a query at somebody else's account (A02). There is no
    ``mode``/``generate``/``rerank``: the read profile is a property of the
    index, and the generative door is not reachable from here at all.
    """

    text: str = Field(min_length=1, max_length=2000)
    library_ids: list[UUID] = Field(default_factory=list, max_length=50)
    limit: int = Field(default=10, ge=1, le=50)


class SearchScope(_Model):
    """One account this caller may search, resolved from the database."""

    library_id: UUID
    account_ref: str
    identity: str
    generation: int = Field(ge=1)
    dimension: int = Field(ge=1)
    embedding_profile: str
    mode: Literal["vectors_only"] = READ_MODE


class SkippedScope(_Model):
    library_id: UUID
    reason: SkipReason
    detail: str | None = None


class IndexHit(_Model):
    """A candidate the index returned.

    Everything here is a *claim*. The library, the account and the generation
    are re-checked against PostgreSQL under the caller's identity before the
    hit is reported, because an index that is fed a poisoned or stale record
    must not be able to widen the caller's scope.
    """

    library_id: UUID
    account_ref: str
    generation: int = Field(ge=1)
    uri: str
    score: float
    snippet: str = Field(max_length=4000)


class SearchPlan(_Model):
    scopes: list[SearchScope] = Field(default_factory=list)
    skipped: list[SkippedScope] = Field(default_factory=list)
    mode: Literal["vectors_only"] = READ_MODE

    @property
    def is_partial(self) -> bool:
        return bool(self.skipped)


class SearchPage(_Model):
    """The result. Carries no generative accounting field, on purpose.

    A ``generative_calls: 0`` constant on this model would be a field that
    says the thing the test is supposed to prove. The proof is the tripwire
    counter in :mod:`kb.retrieval.provisioning_generative`, which stays at
    zero because this module cannot reach it.
    """

    hits: list[IndexHit] = Field(default_factory=list)
    scopes: list[SearchScope] = Field(default_factory=list)
    skipped: list[SkippedScope] = Field(default_factory=list)
    mode: Literal["vectors_only"] = READ_MODE

    @property
    def is_partial(self) -> bool:
        return bool(self.skipped)


@runtime_checkable
class IndexReaderPort(Protocol):
    """The only outbound dependency of a search.

    Read-only by construction: one method, ``find``, and no other. The gateway
    holds the read identity; the identity that can write the index never
    appears on this path.
    """

    def find(self, scopes: list[SearchScope], query: SearchQuery) -> list[IndexHit]: ...


# ------------------------------------------------------------------- routing


def plan_search(conn: psycopg.Connection, principal: Principal, query: SearchQuery) -> SearchPlan:
    """Resolve the accounts this caller may search, and why the rest are out.

    A library the caller cannot see is simply absent — it is not a skipped
    entry, because naming it in the response would already be a disclosure.
    Only libraries the caller *can* read but cannot *search* produce a skip,
    and those are named because the caller already knows they exist.
    """
    library_ids = list(dict.fromkeys(query.library_ids))
    if library_ids:
        rows = _visible_libraries(conn, principal, library_ids)
    else:
        rows = _visible_libraries(conn, principal, None)

    plan = SearchPlan()
    for library_id in rows:
        account = load_account(conn, principal, library_id)
        if account is None:
            plan.skipped.append(SkippedScope(library_id=library_id, reason="not_provisioned"))
            continue
        status = read_index_status(conn, principal, library_id)
        reason = status.skip_reason() if status is not None else "index_behind"
        if reason is not None or status is None or not status.index_ready:
            plan.skipped.append(
                SkippedScope(
                    library_id=library_id,
                    reason=reason or "index_behind",
                    detail=None,
                )
            )
            continue
        plan.scopes.append(_scope_for(account, status))
    return plan


def _scope_for(account: ProvisionedAccount, status: IndexStatus) -> SearchScope:
    return SearchScope(
        library_id=account.library_id,
        account_ref=account.account_ref,
        # The read identity, always. The index identity exists in the same
        # row and is never placed in a search scope: the gateway must not be
        # able to write through the retrieval path even if a bug hands it the
        # wrong string.
        identity=account.read_identity,
        generation=status.current_generation or 1,
        dimension=account.dimension,
        embedding_profile=account.embedding_profile,
    )


def _visible_libraries(
    conn: psycopg.Connection, principal: Principal, library_ids: list[UUID] | None
) -> list[UUID]:
    """Libraries the caller holds any role on. RLS decides; this only narrows.

    The query is a plain SELECT against ``kb.library`` under the caller's
    transaction-local identity, so the rows a principal may not see are gone
    before Python sees them. An explicit list is intersected with it rather
    than trusted: a caller may ask about a library they cannot read, and the
    answer is the same as for one that does not exist.
    """
    query = "SELECT id FROM kb.library"
    params: list[object] = []
    if library_ids:
        query += " WHERE id = ANY(%s)"
        params.append(library_ids)
    query += " ORDER BY id"
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(query, params)
        return [r[0] for r in cur.fetchall()]


# ------------------------------------------------------------------- search


def search_knowledge(
    conn: psycopg.Connection,
    principal: Principal,
    query: SearchQuery,
    reader: IndexReaderPort,
) -> SearchPage:
    """Search the caller's libraries. No generation is spent on this path.

    The order is deliberate and matches ARCHITECTURE §8: resolve the scope
    from PostgreSQL under the caller's identity, ask the index, then re-check
    every hit under that same identity before reporting it. The index is
    consulted only after the ACL question has been answered, and it is never
    believed.
    """
    plan = plan_search(conn, principal, query)
    if not plan.scopes:
        _log.info("search produced no scope", extra={"skipped": len(plan.skipped)})
        return SearchPage(hits=[], scopes=[], skipped=plan.skipped, mode=plan.mode)

    raw_hits = reader.find(plan.scopes, query)
    kept = [hit for hit in raw_hits if _hit_is_ours(conn, principal, plan, hit)]
    dropped = len(raw_hits) - len(kept)
    if dropped:
        # Not an error, but never silent either: an index that returns
        # somebody else's record is an operational fact worth a line.
        _log.warning("discarded index hits that failed re-check", extra={"dropped": dropped})
    return SearchPage(hits=kept, scopes=plan.scopes, skipped=plan.skipped, mode=plan.mode)


def _hit_is_ours(
    conn: psycopg.Connection,
    principal: Principal,
    plan: SearchPlan,
    hit: IndexHit,
) -> bool:
    """Re-check one hit: same library, same account, same generation.

    A hit that fails any of the three is dropped. This is the step that makes
    a poisoned or stale index record a non-event: the index can propose, the
    database disposes.
    """
    scope = next((s for s in plan.scopes if s.library_id == hit.library_id), None)
    if scope is None:
        return False
    if scope.account_ref != hit.account_ref:
        return False
    if scope.generation != hit.generation:
        return False
    # And the caller still holds the library right now. A grant revoked
    # between planning and reporting must take effect before the answer is
    # returned (ACCESS-MODEL §7).
    return _visible_libraries(conn, principal, [hit.library_id]) != []
