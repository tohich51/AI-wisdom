"""C08 — product deactivation, in the order the card requires.

The rule
--------

ACCESS-MODEL section 7: deactivating a person in the product UI first closes
their membership in PostgreSQL and only then revokes their Keycloak sessions.

The order is not a style preference, it is the only order that is safe:

* **PostgreSQL first** means the person is refused the moment the membership
  closes, whether or not Keycloak ever answers. The membership row is what
  every RLS policy consults (see the ``membership_must_be_active`` RESTRICTIVE
  policy in ``migrations/0004_membership.sql``), so a database that says no
  needs no cooperation from anything else.
* **Keycloak second** means the still-valid JWT stops mattering. The token is
  the thing the browser holds; the membership is the thing the server checks.
  A token that has not expired is not a right of access.
* **The other order is a vulnerability.** Revoking the provider session first
  leaves a window — bounded by the token's remaining lifetime — in which a
  completely valid token is accepted and still reads data, and the product has
  promised that it is not.

How the order is kept
---------------------

``deactivate_member()`` is three sequential steps with a transaction boundary
between the first and the second:

1. :func:`_close_membership` — one call to ``kb.deactivate_membership()``, inside
   one transaction, which closes the membership, revokes the gateway's own
   durable browser sessions and writes the journal entry. When that block exits,
   the transaction is COMMITTED.
2. ``revoker.revoke_sessions(...)`` — the only network call, and it can only
   happen after step 1 has committed. There is no code path that reaches it
   with an open transaction, because the ``with`` block has already exited.
3. :func:`_record_provider_answer` — a second, separate transaction recording
   what the provider said, so a failure is outstanding work rather than a lost
   fact.

The test that proves it
-----------------------

``tests/integration/members/test_deactivation_order.py`` does not read a comment
or an attribute. Its stand-in revoker opens its OWN connection to the same real
PostgreSQL and reads ``kb.membership.status`` at the moment it is called, so it
observes exactly what any other connection in the world would observe: the
committed value. If the two steps were swapped, the recorder would see
``active`` and the assertion would fail.

What is NOT here
----------------

Keycloak. There is none in this environment (E03 pending), so the real client
lives in :mod:`kb.access.membership_keycloak` and is exercised by nothing. It
is written against Keycloak's documented admin API and is reported as
``not_run``, never as a pass.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from kb.access.membership import Membership, identity_cursor
from kb.access.policy import Principal

log = logging.getLogger(__name__)

UTC = dt.UTC


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class RevocationOutcome:
    """What the identity provider said.

    ``revoked`` is false when the provider did not confirm. It is never
    optimistically true, and it is never a guess: the caller of a revocation
    that did not happen must be able to tell from this object that it did not.
    """

    revoked: bool
    #: A short, closed-vocabulary reason. Never an upstream response body: this
    #: value reaches a journal row and an HTTP response, and an error body from
    #: a provider is exactly the place where a secret or a token can echo out.
    reason: str
    detail: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class SessionRevoker(Protocol):
    """The second half of the deactivation.

    One method, so the ordering guarantee has exactly one place it can be
    broken, and so a test can observe the call without standing up an identity
    provider. An implementation MUST NOT touch PostgreSQL membership: that
    would move the authority for the decision out of the database, and this
    module's whole argument is that PostgreSQL decides first.
    """

    def revoke_sessions(self, *, issuer: str, subject: str) -> RevocationOutcome:
        """End every live provider session for one (issuer, subject)."""
        ...


class DeactivationResult(_Model):
    """What happened, in the order it happened.

    ``membership_closed_at`` is the commit time of the PostgreSQL step and
    ``provider_revocated_at`` is when the provider answered. The two are both
    present even when the second failed, because "the membership is closed and
    the provider never confirmed" is the fact an operator needs — not a partial
    success.
    """

    organisation_id: UUID
    principal_id: UUID
    #: The revision the close committed under. Any request admitted under an
    #: older revision has to re-check before it delivers.
    policy_revision: int
    membership_status: str
    membership_closed_at: dt.datetime
    browser_sessions_closed: int
    provider_revocated: bool
    provider_reason: str | None = None
    provider_revocated_at: dt.datetime | None = None
    #: True when the provider did not confirm. The member is refused either
    #: way; this flag is the operator's work list, nothing more.
    revocation_outstanding: bool = False
    membership: Membership | None = None


def _close_membership(
    conn: psycopg.Connection,
    actor: Principal,
    *,
    organisation_id: UUID,
    target: UUID,
    reason: str | None,
) -> tuple[int, Membership]:
    """PHASE 1. Commit this transaction before anything else happens.

    The ``with`` block is the whole of the guarantee: ``transaction_identity``
    commits on the way out, so by the time this function returns, another
    connection in any other process sees a closed membership.
    """
    with identity_cursor(conn, actor) as cur:
        cur.execute(
            "SELECT kb.deactivate_membership(%s, %s, %s)", (organisation_id, target, reason)
        )
        row = cur.fetchone()
        if row is None:  # pragma: no cover - the function returns or raises
            raise RuntimeError("deactivate_membership returned no revision")
        revision = int(row[0])

        cur.execute(
            "SELECT principal_id, organisation_id, issuer, subject, display_name, email, "
            "       status, invited_at, activated_at, deactivated_at, deactivated_by, "
            "       deactivation_reason "
            "  FROM kb.membership WHERE organisation_id = %s AND principal_id = %s",
            (organisation_id, target),
        )
        found = cur.fetchone()
    if found is None:  # pragma: no cover - the row was just updated
        raise RuntimeError("membership vanished immediately after deactivation")
    membership = Membership(
        principal_id=found[0],
        organisation_id=found[1],
        issuer=found[2],
        subject=found[3],
        display_name=found[4],
        email=found[5],
        status=found[6],
        invited_at=found[7],
        activated_at=found[8],
        deactivated_at=found[9],
        deactivated_by=found[10],
        deactivation_reason=found[11],
    )
    if membership.deactivated_at is None:  # pragma: no cover - a table CHECK forbids this
        raise RuntimeError("membership is not deactivated although the call returned")
    return revision, membership


def _record_provider_answer(
    conn: psycopg.Connection,
    actor: Principal,
    *,
    organisation_id: UUID,
    target: UUID,
    outcome: RevocationOutcome,
) -> int:
    """PHASE 3. A second transaction, after the provider has answered.

    Separate on purpose. If this were part of the first transaction the provider
    call would be inside it, and a Keycloak timeout would roll back a
    deactivation that has to stand.
    """
    with identity_cursor(conn, actor) as cur:
        cur.execute(
            "SELECT kb.record_session_revocation(%s, %s, %s, %s)",
            (
                organisation_id,
                target,
                "revoked" if outcome.revoked else "failed",
                # Jsonb(), not a bare dict: psycopg will not adapt a Python
                # mapping to an unknown parameter type, and a silently stringified
                # detail object would be a journal nobody can query.
                Jsonb(_journal_detail(outcome)),
            ),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


#: The only keys a revoker may put into the journal. A closed vocabulary, not
#: a type filter.
#:
#: A type filter ("carry scalars and short strings") is not enough: a provider
#: dict may hold ``{"token": "<a real bearer token>"}`` and that value is a
#: short string. What reaches a journal that an administrator can read, months
#: later, has to be a key this file already decided was safe to keep.
JOURNALLED_DETAIL_KEYS: Final[frozenset[str]] = frozenset(
    {
        # a provider status code, e.g. 204
        "status",
        # how many sessions the provider says it ended
        "sessions",
        # the host of the issuer, never the issuer itself: an issuer URL is an
        # identity coordinate and a journal is not where they accumulate
        "issuer_host",
    }
)
_MAX_DETAIL_CHARS: Final[int] = 200


def _journal_detail(outcome: RevocationOutcome) -> dict[str, object]:
    """A bounded, JSON-safe view of the outcome, restricted to known keys."""
    detail: dict[str, object] = {"reason": outcome.reason[:_MAX_DETAIL_CHARS]}
    for key in JOURNALLED_DETAIL_KEYS:
        if key not in outcome.detail:
            continue
        value = outcome.detail[key]
        if isinstance(value, bool | int | float) or value is None:
            detail[key] = value
        elif isinstance(value, str):
            detail[key] = value[:_MAX_DETAIL_CHARS]
    return detail


def browser_sessions_closed(conn: psycopg.Connection, principal: Principal, target: UUID) -> int:
    """How many gateway sessions the close killed. For the API response."""
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT count(*) FROM kb.browser_session "
            "WHERE principal_id = %s AND revoked_at IS NOT NULL "
            "  AND revoked_reason = 'membership_deactivated'",
            (target,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def deactivate_member(
    conn: psycopg.Connection,
    actor: Principal,
    *,
    organisation_id: UUID,
    target: UUID,
    revoker: SessionRevoker,
    reason: str | None = None,
    now: dt.datetime | None = None,
) -> DeactivationResult:
    """Block a member, then revoke their provider sessions. In that order.

    ``actor`` is who is doing it and is taken from the transport; ``target`` is
    who it is done to and is a path parameter of the administrator's action.
    Neither can be supplied by a request body, and the database refuses a
    non-administrator trying to block somebody else.

    A provider that fails does not undo the block and does not raise: the
    membership is closed, the person is refused, and the returned result says
    ``revocation_outstanding`` so an operator can retry. Raising here would mean
    a caller could tell "blocked" from "not blocked" by how the request failed,
    and would tempt a retry that re-runs phase 1 against a membership that is
    already closed.
    """
    if target is None:  # pragma: no cover - a path parameter is always present
        raise ValueError("a deactivation needs a target principal")

    # ---- PHASE 1: PostgreSQL. Committed on the way out of this block. ----
    revision, membership = _close_membership(
        conn, actor, organisation_id=organisation_id, target=target, reason=reason
    )
    closed_at = membership.deactivated_at or (now or dt.datetime.now(UTC))

    # ---- PHASE 2: the provider. Unreachable before the commit above. ----
    try:
        outcome = revoker.revoke_sessions(issuer=membership.issuer, subject=membership.subject)
    except Exception as exc:
        # The class name only. The message could contain a URL with a token in
        # it, and this line goes to a log.
        log.warning(
            "provider session revocation failed for principal %s: %s",
            target,
            type(exc).__name__,
        )
        outcome = RevocationOutcome(revoked=False, reason="provider_unreachable")

    # ---- PHASE 3: the record, in its own transaction. ----
    _record_provider_answer(
        conn,
        actor,
        organisation_id=organisation_id,
        target=target,
        outcome=outcome,
    )

    return DeactivationResult(
        organisation_id=organisation_id,
        principal_id=target,
        policy_revision=revision,
        membership_status=membership.status.value,
        membership_closed_at=closed_at,
        browser_sessions_closed=browser_sessions_closed(conn, actor, target),
        provider_revocated=outcome.revoked,
        provider_reason=outcome.reason,
        provider_revocated_at=(now or dt.datetime.now(UTC)),
        revocation_outstanding=not outcome.revoked,
        membership=membership,
    )


# ================================================================== retries


def outstanding_revocations(
    conn: psycopg.Connection, actor: Principal, organisation_id: UUID
) -> list[UUID]:
    """Members whose provider revocation has not been confirmed.

    "A failure was recorded and nothing since" — the journal is the only record
    of either, and the comparison is by revision, so a later success clears an
    earlier failure without anybody editing a row.
    """
    with identity_cursor(conn, actor) as cur:
        cur.execute(
            """
            SELECT DISTINCT failed.subject_principal_id
              FROM kb.access_policy_journal failed
             WHERE failed.organisation_id = %s
               AND failed.action = 'sessions_revocation_failed'
               AND failed.subject_principal_id IS NOT NULL
               AND NOT EXISTS (
                     SELECT 1 FROM kb.access_policy_journal later
                      WHERE later.organisation_id = failed.organisation_id
                        AND later.subject_principal_id = failed.subject_principal_id
                        AND later.action = 'sessions_revoked'
                        AND later.revision > failed.revision
               )
             ORDER BY 1
            """,
            (organisation_id,),
        )
        return [row[0] for row in cur.fetchall()]


def retry_outstanding_revocations(
    conn: psycopg.Connection,
    actor: Principal,
    *,
    organisation_id: UUID,
    revoker: SessionRevoker,
) -> list[DeactivationResult]:
    """Re-attempt the provider call for members whose block already stands.

    Phase 1 is NOT repeated. The membership is closed; only the second half of
    the sequence was outstanding, and re-running the first would fail against a
    membership that is no longer active — which is the correct behaviour, and
    the reason this function is not just ``deactivate_member`` in a loop.
    """
    results: list[DeactivationResult] = []
    for target in outstanding_revocations(conn, actor, organisation_id):
        with identity_cursor(conn, actor) as cur:
            cur.execute(
                "SELECT principal_id, organisation_id, issuer, subject, display_name, email, "
                "       status, invited_at, activated_at, deactivated_at, deactivated_by, "
                "       deactivation_reason "
                "  FROM kb.membership WHERE organisation_id = %s AND principal_id = %s",
                (organisation_id, target),
            )
            row = cur.fetchone()
        if row is None:  # pragma: no cover - the journal referenced it a moment ago
            continue
        membership = Membership(
            principal_id=row[0],
            organisation_id=row[1],
            issuer=row[2],
            subject=row[3],
            display_name=row[4],
            email=row[5],
            status=row[6],
            invited_at=row[7],
            activated_at=row[8],
            deactivated_at=row[9],
            deactivated_by=row[10],
            deactivation_reason=row[11],
        )
        try:
            outcome = revoker.revoke_sessions(issuer=membership.issuer, subject=membership.subject)
        except Exception as exc:
            log.warning("provider revocation retry failed for %s: %s", target, type(exc).__name__)
            outcome = RevocationOutcome(revoked=False, reason="provider_unreachable")
        _record_provider_answer(
            conn,
            actor,
            organisation_id=organisation_id,
            target=target,
            outcome=outcome,
        )
        results.append(
            DeactivationResult(
                organisation_id=organisation_id,
                principal_id=target,
                policy_revision=current_revision(conn, actor),
                membership_status=membership.status.value,
                membership_closed_at=membership.deactivated_at or dt.datetime.now(UTC),
                browser_sessions_closed=browser_sessions_closed(conn, actor, target),
                provider_revocated=outcome.revoked,
                provider_reason=outcome.reason,
                provider_revocated_at=dt.datetime.now(UTC),
                revocation_outstanding=not outcome.revoked,
                membership=membership,
            )
        )
    return results


def current_revision(conn: psycopg.Connection, actor: Principal) -> int:
    with identity_cursor(conn, actor) as cur:
        cur.execute("SELECT kb.current_policy_revision()")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def never_configured() -> RevocationOutcome:
    """What the gateway answers when no revoker was wired in.

    There is no default Keycloak client, and inventing one would be a mock
    presented as a service. A deployment that has not configured a revoker gets
    this, which is a *failure*: the membership is closed and the outstanding
    work is visible in the journal.
    """
    return RevocationOutcome(revoked=False, reason="no_revoker_configured")


def sequence_names(revoker: SessionRevoker | None) -> Sequence[str]:
    """The steps that will run, for the startup log. Never raises."""
    if revoker is None:
        return ("postgresql:membership_closed", "provider:not_configured")
    return ("postgresql:membership_closed", f"provider:{type(revoker).__name__}")


__all__ = [
    "DeactivationResult",
    "RevocationOutcome",
    "SessionRevoker",
    "browser_sessions_closed",
    "current_revision",
    "deactivate_member",
    "never_configured",
    "outstanding_revocations",
    "retry_outstanding_revocations",
    "sequence_names",
]
