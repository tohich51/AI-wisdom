"""C15 — the generation registry and the policy that guards it.

Three product decisions live here, and all three are enforced by the database
first (see ``migrations/0004_provisioning.sql``) and expressed in Python
second, so that the Python cannot be the only thing standing between a read
and somebody's subscription quota:

* **Opt-in.** ``kb.generation_policy.generation_allowed`` is false until the
  owner of the library sets it. A library with no policy row has no opt-in at
  all, which is the same answer.
* **Owner-started.** A read never opens a generation. The insert policy
  requires the library's own opt-in *and* a principal the policy admits, so
  "a colleague asked for it" is not a permission to spend the owner's quota.
* **One at a time, installation-wide.** A partial unique index over the
  constant ``'building'`` allows exactly one open generation in the whole
  database. It is not a check this module performs; two callers racing here
  collide on the index and one of them loses.

The last one is also the answer to "a half-finished reindex is detectable".
The registry distinguishes three states a naive boolean would flatten:

======================  ==================================================
``building`` present    a rebuild is open. The job may have died; nothing
                        has moved the row, and the library is not ready.
current < library        the content moved and the index has not caught up.
                        A11: withdrawn text must not be served.
current == library      the index answers for the current content.
======================  ==================================================

:func:`read_index_status` reports exactly those four numbers and nothing
operational, so a reader can be told "your library is not ready" without being
told who started a rebuild, when, or which canary it wrote.
"""

from __future__ import annotations

import logging
from typing import Final, Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kb.access.policy import Principal, transaction_identity

_log = logging.getLogger("kb.retrieval.generations")

#: Why a library's index is not usable. A closed vocabulary, because this text
#: is what the UI shows and what a search reports as `partial`; a free string
#: here would drift between the two.
SkipReason = Literal[
    "not_provisioned",  # no account mapping yet
    "rebuild_in_progress",  # a generation is open
    "index_behind",  # the library's content is newer than the index
]

#: PRODUCT-SPEC: "Сначала запуск генерации владельцем, общий concurrency 1."
#: The column exists in 0001 and defaults to 1. It is enforced at 1 here
#: rather than read, because a stored 3 that the one-slot index refuses to
#: honour is a lie in a column.
GLOBAL_CONCURRENCY: Final = 1


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndexStatus(_Model):
    """What a search is allowed to know about a library's index.

    Four numbers, no identifiers, no URIs, no principal. A reader below the
    operational read policy still gets these, because "the index does not
    match your library" is a correctness fact about their own data and
    withholding it would produce a silently wrong answer.
    """

    library_id: UUID
    library_generation: int = Field(ge=1)
    current_generation: int | None = Field(default=None, ge=1)
    building_generation: int | None = Field(default=None, ge=1)
    index_ready: bool

    def skip_reason(self) -> SkipReason | None:
        """Why this library must not be searched, or None when it may be.

        Order matters: an open rebuild is reported as such even when the
        current generation still matches, because a build in flight means the
        projection is being rewritten under the search.
        """
        if self.building_generation is not None:
            return "rebuild_in_progress"
        if self.current_generation is None or self.current_generation < self.library_generation:
            return "index_behind"
        return None


class GenerationPolicy(_Model):
    library_id: UUID
    generation_allowed: bool
    max_concurrency: int = Field(ge=1)
    requires_owner_start: bool


class AdmittedGeneration(_Model):
    """The outcome of asking for a generation.

    ``resumed`` distinguishes "this build was already open and I handed you
    its number" from "I opened a new one". Both are successes; only the second
    spends a slot.
    """

    library_id: UUID
    generation: int = Field(ge=1)
    resumed: bool
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SetGenerationPolicy(_Model):
    """The owner's opt-in. No principal field: identity comes from the
    transport, and a request model that carried one would be a request model
    that could lie about who is asking."""

    generation_allowed: bool
    requires_owner_start: bool = True
    max_concurrency: int = GLOBAL_CONCURRENCY

    @field_validator("max_concurrency")
    @classmethod
    def _one_at_a_time(cls, value: int) -> int:
        # Refused here rather than clamped: silently writing 1 when the
        # operator asked for 2 would make the stored value disagree with the
        # request they made, which is the same class of lie as a policy column
        # that promises more than the one-slot index delivers.
        if value != GLOBAL_CONCURRENCY:
            raise ValueError(
                "generation concurrency is fixed at 1 for the whole installation "
                "(PRODUCT-SPEC: общий concurrency 1); it is a database constraint, "
                "not a setting"
            )
        return value


class GenerationPermit(_Model):
    """Authority to spend one generation. Minted from the live registry.

    This is an object rather than a boolean for one reason: a boolean can be
    written down by whoever calls it. A permit can only be produced by
    :func:`permit_for_generation`, which reads the row and confirms it is
    still open. ``kb.retrieval.provisioning_generative.generate`` accepts this
    type and nothing else, so "a caller asserted that it was allowed to spend
    the owner's subscription" is not a state the code can be in.
    """

    library_id: UUID
    generation: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose_agnostic: bool = True


class GenerationRefused(Exception):
    """The policy says no.

    The message never says whether the library exists: "forbidden" and "does
    not exist" are the same answer, because the difference is a directory of
    objects the caller may not know about.
    """


def _translate(exc: psycopg.Error) -> GenerationRefused:
    """Turn PostgreSQL's answer into ours, without adding information."""
    text = str(exc).lower()
    if "row-level security" in text or "permission denied" in text:
        return GenerationRefused("forbidden")
    if "index_generation_one_slot_installation_wide" in text:
        return GenerationRefused(
            "another library holds the single generation slot; "
            "generation runs one at a time for the whole installation"
        )
    if "index_generation_one_live_per_content_hash" in text:
        return GenerationRefused("a generation for this content is already open or current")
    if "insufficient_privilege" in text:
        return GenerationRefused("forbidden")
    return GenerationRefused("generation refused")


# ------------------------------------------------------------------ reading


def read_index_status(
    conn: psycopg.Connection, principal: Principal, library_id: UUID
) -> IndexStatus | None:
    """The four numbers. None when the caller holds nothing on the library.

    ``kb.index_status`` returns no row at all for a principal without a role,
    so a caller cannot use this to discover which library ids exist.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute("SELECT * FROM kb.index_status(%s)", (library_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return IndexStatus(
        library_id=library_id,
        library_generation=row[0],
        current_generation=row[1],
        building_generation=row[2],
        index_ready=row[3],
    )


def read_generation_policy(
    conn: psycopg.Connection, principal: Principal, library_id: UUID
) -> GenerationPolicy | None:
    """The library's opt-in, or None when it has never been set.

    None is not "false by another route": a library with no row has no opt-in,
    and admission refuses it for exactly that reason.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT library_id, generation_allowed, max_concurrency, requires_owner_start "
            "FROM kb.generation_policy WHERE library_id = %s",
            (library_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return GenerationPolicy(
        library_id=row[0],
        generation_allowed=row[1],
        max_concurrency=row[2],
        requires_owner_start=row[3],
    )


# ------------------------------------------------------------------ writing


def set_generation_policy(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    spec: SetGenerationPolicy,
) -> GenerationPolicy:
    """Owner opt-in. Manager-only, enforced by RLS.

    Both shapes of refusal are handled and they are different. An INSERT whose
    ``WITH CHECK`` fails is *rejected* — PostgreSQL raises — and that arrives
    here as an error. An UPDATE under RLS is *filtered*: a curator's write
    reports zero rows and raises nothing. Zero rows is therefore treated as a
    refusal, because the C06 suite documents exactly how silent a filtered
    write is.
    """
    try:
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kb.generation_policy
                    (library_id, generation_allowed, max_concurrency, requires_owner_start)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (library_id) DO UPDATE
                    SET generation_allowed = EXCLUDED.generation_allowed,
                        max_concurrency = EXCLUDED.max_concurrency,
                        requires_owner_start = EXCLUDED.requires_owner_start
                """,
                (
                    library_id,
                    spec.generation_allowed,
                    spec.max_concurrency,
                    spec.requires_owner_start,
                ),
            )
            if cur.rowcount == 0:
                raise GenerationRefused("forbidden")
    except GenerationRefused:
        raise
    except psycopg.Error as exc:
        raise _translate(exc) from exc
    written = read_generation_policy(conn, principal, library_id)
    if written is None:  # pragma: no cover - the insert above either lands or raises
        raise GenerationRefused("forbidden")
    return written


def admit_rebuild(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    content_hash: str,
) -> AdmittedGeneration:
    """Open one generation for a library, if the policy allows it.

    Idempotent by content: the same content on a library that already has it
    open or current hands back that generation with ``resumed=True`` and
    spends nothing. A *failed* generation is not resumed — a retry is a new
    attempt with its own number, so the journal shows what happened instead of
    overwriting the failure.
    """
    try:
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute("SELECT * FROM kb.admit_index_rebuild(%s, %s)", (library_id, content_hash))
            row = cur.fetchone()
    except psycopg.errors.InsufficientPrivilege as exc:
        raise _translate(exc) from exc
    except psycopg.Error as exc:
        raise _translate(exc) from exc
    if row is None:  # pragma: no cover - the function always returns a row
        raise GenerationRefused("forbidden")
    return AdmittedGeneration(
        library_id=library_id, generation=row[0], resumed=row[1], content_hash=content_hash
    )


def publish_generation(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    generation: int,
    canary_uri: str | None = None,
) -> None:
    """Publish a finished build: retire the old current one, make this current.

    Both updates happen in one transaction, and the one-current-per-library
    index is what makes that atomicity mean something. A library can never be
    serving two generations at once, not even for the length of a statement.
    """
    try:
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(
                "SELECT kb.publish_index_generation(%s, %s, %s)",
                (library_id, generation, canary_uri),
            )
    except psycopg.Error as exc:
        raise _translate(exc) from exc


def fail_generation(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    generation: int,
    reason: str,
) -> None:
    """Retire a build that died, with the operator-visible reason attached.

    The reason is required. A failed generation with no explanation is exactly
    the half-finished state this card exists to make visible, and 'it stopped'
    is not a reason.
    """
    try:
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(
                "SELECT kb.fail_index_generation(%s, %s, %s)", (library_id, generation, reason)
            )
    except psycopg.Error as exc:
        raise _translate(exc) from exc


def mint_permit(library_id: UUID, generation: int, content_hash: str) -> GenerationPermit:
    """Build a permit from values the caller has already been given.

    Exposed for the case where the registry row was read moments ago in the
    same transaction; :func:`permit_for_generation` is the checked path and is
    what tests and callers should use.
    """
    return GenerationPermit(library_id=library_id, generation=generation, content_hash=content_hash)


def permit_for_generation(
    conn: psycopg.Connection,
    principal: Principal,
    library_id: UUID,
    generation: int,
) -> GenerationPermit | None:
    """The permit for an open generation, or None when there is not one.

    Reads the registry as the caller. A generation that is not ``building``
    yields None, so a permit cannot outlive the build it was minted for: a
    build that finished or failed between minting and spending is refused.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT content_hash
              FROM kb.index_generation
             WHERE library_id = %s AND generation = %s AND state = 'building'
            """,
            (library_id, generation),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return mint_permit(library_id, generation, row[0])


def list_generations(
    conn: psycopg.Connection, principal: Principal, library_id: UUID
) -> list[dict[str, object]]:
    """The registry rows for a library, newest first. Operational detail.

    Requires contributor (0002's ``index_generation_read``), which is why a
    plain reader uses :func:`read_index_status` instead. Every field is None
    rather than filled in when it is unknown: ``canary_uri`` is None until a
    build publishes one, ``failure_reason`` is None for a build that did not
    fail.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT generation, state, canary_uri, started_at, finished_at,
                   started_by, content_hash, attempt, failure_reason
              FROM kb.index_generation
             WHERE library_id = %s
             ORDER BY generation DESC
            """,
            (library_id,),
        )
        rows = cur.fetchall()
    return [
        {
            "generation": r[0],
            "state": r[1],
            "canary_uri": r[2],
            "started_at": r[3],
            "finished_at": r[4],
            "started_by": r[5],
            "content_hash": r[6],
            "attempt": r[7],
            "failure_reason": r[8],
        }
        for r in rows
    ]
