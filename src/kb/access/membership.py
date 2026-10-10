"""C08 — membership, groups, invitations, and the policy journal.

What this module is for
-----------------------

The product is invite-only. Somebody is *not* a member of the knowledge base
because they have an account at the identity provider; they are a member
because a row in ``kb.membership`` says so, and deactivation is the act of
closing that row. Everything in here is the application half of that, and every
one of these operations is a translation of a stored procedure in
``migrations/0004_membership.sql`` — the database holds the rule, this module
holds the vocabulary and the typing.

What this module is not
-----------------------

It is not an access decision. ``kb.effective_role`` (re-defined in 0004 to union
the direct and the group path) is what decides, and the RLS policies call it.
The model here is free to *report* a principal's access; it is free to
``authorize`` nothing, and the only way it stops anybody is by asking the
database, which then refuses.

Ordering
--------

Deactivation is the one operation with a required order: close the membership in
PostgreSQL **first**, revoke the Keycloak sessions **second**. That lives in
:mod:`kb.access.membership_deactivation` rather than here, because it is a
sequence of two systems and burying it in a file of CRUD helpers is how a
sequence gets reordered by the next person who edits this one.

Identity
--------

Every function here takes a :class:`kb.access.policy.Principal` that the trusted
transport produced, and every SQL statement runs inside
``transaction_identity()``, so ``app.principal`` is the only source of identity
in the session. No request model in this module has a ``user_id``, an ``actor``
or a ``role`` field that would let a caller speak for somebody else: a
``principal_id`` in a *path* is the subject of an administrator's action, and a
``role`` in a *body* is the role a grant confers, which is the decision itself
and not a claim about the caller.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import AccessDenied, Principal, transaction_identity

UTC = dt.UTC


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ============================================================== vocabulary


class MembershipStatus(StrEnum):
    """The two states a membership can be in.

    Mirrors ``CHECK (status IN ('active','deactivated'))`` on ``kb.membership``.
    There is no 'suspended': the card asks for blocking, and a blocked member
    stays blocked until somebody invites them again. A richer taxonomy would be
    a guess about a product decision nobody has taken.
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class AccessPathKind(StrEnum):
    """Which kind of grant is holding.

    The distinction is the point of A08: with no explicit deny in v1, removing a
    person from a group does not remove their direct grant, and the UI has to be
    able to say which one is still there.
    """

    DIRECT = "direct"
    GROUP = "group"


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"


class DeliveryState(StrEnum):
    """How far an invitation got. On this stand: never further than pending.

    ``OPERATOR_PENDING`` is the resting state and it is not a placeholder — it
    is the fact that a human still has to tell the person, and the product has no
    transport of its own to tell them with.
    """

    OPERATOR_PENDING = "operator_pending"
    NOTIFIED = "notified"


class DeniedBecause(StrEnum):
    """A closed vocabulary of *why* an explanation says no.

    A reason code, not a sentence. The UI is Russian (PRODUCT-SPEC) and this
    domain layer is not, so the wording is the caller's to supply and the reason
    code is what travels.
    """

    NO_PATH = "no_path"
    MEMBERSHIP_CLOSED = "membership_closed"
    #: The caller may not ask. Distinct from NO_PATH on purpose: "they have no
    #: access" and "you may not look" are different answers, and answering the
    #: second with the first would tell an organisation administrator that a
    #: private library they cannot open is empty.
    NOT_VISIBLE = "not_visible"


class MembershipInactive(PermissionError):
    """The caller has a verified identity and no live membership.

    Distinct from :class:`AccessDenied` on purpose. ``AccessDenied`` says "not
    yours" and hides whether the object exists. This one says "you are not a
    member of this installation", which the person is entitled to know and which
    the UI has to render differently from a permission error.
    """


# =================================================================== models


class Membership(_Model):
    """One row of ``kb.membership``, as the API may show it.

    ``email`` and ``display_name`` are ``None`` when nobody wrote them down.
    They are never 'unknown' and never '': an empty string is a claim that the
    person has no name, and NULL is a claim that we do not know.
    """

    principal_id: UUID
    organisation_id: UUID
    issuer: str
    subject: str
    display_name: str | None = None
    email: str | None = None
    status: MembershipStatus
    invited_at: dt.datetime
    activated_at: dt.datetime
    deactivated_at: dt.datetime | None = None
    deactivated_by: UUID | None = None
    deactivation_reason: str | None = None


class MembershipBlock(_Model):
    """Why the membership gate is closed, if it is."""

    principal_id: UUID
    blocked: bool
    status: MembershipStatus | None = None
    deactivated_at: dt.datetime | None = None
    deactivated_by: UUID | None = None


class AccessPath(_Model):
    """One way in. A direct grant, or a group the person is still in."""

    kind: AccessPathKind
    role: str
    group_id: UUID | None = None
    group_name: str | None = None


class AccessExplanation(_Model):
    """The A08 answer.

    Not a boolean. When somebody is removed from a group and keeps a direct
    grant, this says so, names the group that is no longer holding, and says
    which path is. A bare "forbidden" here would tell the person to stop asking
    while their own direct grant is still working.
    """

    library_id: UUID
    principal_id: UUID
    effective_role: str | None = None
    paths: list[AccessPath] = Field(default_factory=list)
    denied: bool
    reason: DeniedBecause
    # The policy revision this answer was computed at. A UI that renders a
    # grant list is rendering a snapshot, and saying which snapshot is the
    # difference between a list and a guess.
    policy_revision: int = Field(ge=0)


class Invitation(_Model):
    """A recorded intent to invite somebody. Never a delivered message."""

    id: UUID
    organisation_id: UUID
    email: str | None = None
    subject: str | None = None
    status: InvitationStatus
    delivery_state: DeliveryState
    principal_id: UUID | None = None
    invited_at: dt.datetime
    expires_at: dt.datetime | None = None
    notified_at: dt.datetime | None = None
    accepted_at: dt.datetime | None = None
    withdrawn_at: dt.datetime | None = None
    # True while a human still has to act. The API never reports an invitation
    # as sent, because nothing in this product sends one.
    awaiting_operator: bool


class JournalEntry(_Model):
    """One append-only line of the policy journal."""

    revision: int
    action: str
    occurred_at: dt.datetime
    subject_principal_id: UUID | None = None
    actor_principal_id: UUID | None = None
    library_id: UUID | None = None
    group_id: UUID | None = None
    invitation_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class AccessGroup(_Model):
    id: UUID
    organisation_id: UUID
    name: str
    created_at: dt.datetime


class GroupMember(_Model):
    group_id: UUID
    principal_id: UUID
    added_at: dt.datetime


class GroupGrant(_Model):
    group_id: UUID
    library_id: UUID
    role: str
    granted_at: dt.datetime


# ============================================================== plumbing


@contextlib.contextmanager
def identity_cursor(conn: psycopg.Connection, principal: Principal):
    """One transaction, one identity, one cursor.

    Every statement in this module runs inside this, which is what makes
    ``app.principal`` — and therefore every RLS policy — refer to the caller and
    nobody else. A statement issued outside it would run as the connection's
    last user, which is the failure mode C06's pool test is about.
    """
    with transaction_identity(conn, principal) as txn, txn.cursor() as cur:
        yield cur


def _first(cur: psycopg.Cursor) -> tuple | None:
    return cur.fetchone()


# ================================================================= the gate


def current_policy_revision(conn: psycopg.Connection) -> int:
    """The organisation-wide fence, as a single integer.

    ACCESS-MODEL §7: a policy change is committed together with an increment,
    and a request admitted before that commit must be re-checked before it is
    allowed to deliver. A caller that holds an older revision than the one it
    was admitted under re-runs its membership check; the cost of a stale answer
    is one extra SELECT, and the cost of not doing it is a long request that
    delivers content to somebody who was removed from the installation while it
    was running.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT kb.current_policy_revision()")
        row = _first(cur)
    return int(row[0]) if row else 0


def block_state(
    conn: psycopg.Connection, principal: Principal, *, target: UUID | None = None
) -> MembershipBlock:
    """Is this principal's membership closed, and when did it close?

    Read through the caller's own transaction identity, so it answers about
    the caller unless an administrator asks about somebody else. An
    administrator asking still gets a *state*, never a library's contents: this
    is the gate's own question and it is deliberately narrower than "what can
    this person see".
    """
    who = target or principal.principal_id
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT status, activated_at, deactivated_at, deactivated_by, deactivation_reason "
            "  FROM kb.membership "
            " WHERE principal_id = %s "
            "   AND (principal_id = kb.current_principal() "
            "        OR kb.is_org_admin(organisation_id, kb.current_principal())) "
            " ORDER BY activated_at DESC LIMIT 1",
            (who,),
        )
        row = _first(cur)
    if row is None:
        # No membership row at all is not a block: this principal never went
        # through the invite lifecycle. It is not the same as being active, and
        # no library access follows from it either way.
        return MembershipBlock(principal_id=who, blocked=False)
    return MembershipBlock(
        principal_id=who,
        blocked=row[0] == MembershipStatus.DEACTIVATED.value,
        status=MembershipStatus(row[0]),
        deactivated_at=row[2],
        deactivated_by=row[3],
    )


def require_active_membership(
    conn: psycopg.Connection,
    principal: Principal,
    *,
    revision: int | None = None,
) -> None:
    """Raise :class:`MembershipInactive` if the caller's membership is closed.

    This is the application-level half of A16 and it is deliberately *not* the
    only half. The RESTRICTIVE policy ``membership_must_be_active`` in 0004
    already refuses every row to a deactivated principal, so a caller who
    somehow reached a query would get nothing back. This function exists so the
    answer is a clear 403 with a reason instead of an empty list that looks
    like an empty account.

    ``revision`` is the policy revision the caller's request was admitted
    under. A revision older than the current one means the world moved while
    this request was running, and the state is re-read for that reason rather
    than trusted from admission time: the "a long request re-checks before it
    delivers" half of ACCESS-MODEL section 7. The read is cheap and the
    alternative is a long request delivering content to somebody who was
    removed from the installation while it ran.
    """
    if revision is not None and revision < current_policy_revision(conn):
        pass  # stale on purpose: fall through to the fresh read below
    state = block_state(conn, principal)
    if state.blocked:
        raise MembershipInactive("membership_deactivated")


# ============================================================= memberships


_MEMBERSHIP_COLUMNS = (
    "principal_id, organisation_id, issuer, subject, display_name, email, status, "
    "invited_at, activated_at, deactivated_at, deactivated_by, deactivation_reason"
)


def _membership_from_row(row: tuple) -> Membership:
    return Membership(
        principal_id=row[0],
        organisation_id=row[1],
        issuer=row[2],
        subject=row[3],
        display_name=row[4],
        email=row[5],
        status=MembershipStatus(row[6]),
        invited_at=row[7],
        activated_at=row[8],
        deactivated_at=row[9],
        deactivated_by=row[10],
        deactivation_reason=row[11],
    )


def get_membership(
    conn: psycopg.Connection, principal: Principal, organisation_id: UUID, who: UUID
) -> Membership | None:
    """One membership, or None — which is also the answer for "not yours"."""
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            sql.SQL(
                "SELECT {} FROM kb.membership WHERE organisation_id = %s AND principal_id = %s"
            ).format(sql.SQL(_MEMBERSHIP_COLUMNS)),
            (organisation_id, who),
        )
        row = _first(cur)
    return _membership_from_row(row) if row else None


def list_memberships(
    conn: psycopg.Connection, principal: Principal, organisation_id: UUID
) -> list[Membership]:
    """Everybody the caller is entitled to see.

    RLS has already drawn the line: ``membership_read`` admits the caller's own
    row and the rows an organisation administrator may see, and nothing else.
    There is no ``WHERE organisation_id = %s`` in Python that could widen it —
    the filter is the same one, and the database is the one that applies it.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            sql.SQL(
                "SELECT {} FROM kb.membership "
                "WHERE organisation_id = %s ORDER BY invited_at, principal_id"
            ).format(sql.SQL(_MEMBERSHIP_COLUMNS)),
            (organisation_id,),
        )
        return [_membership_from_row(r) for r in cur.fetchall()]


def add_member(
    conn: psycopg.Connection,
    principal: Principal,
    *,
    organisation_id: UUID,
    who: UUID,
    issuer: str,
    subject: str,
    display_name: str | None = None,
    email: str | None = None,
) -> UUID:
    """Put somebody in the organisation directly, without an invitation.

    For a person who already has a verified identity and whose invitation was
    settled outside the product. The actor must be an organisation
    administrator; the database refuses anybody else before a row exists.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT kb.create_membership(%s, %s, %s, %s, %s, %s, NULL)",
            (organisation_id, who, issuer, subject, display_name, email),
        )
        row = _first(cur)
    if row is None:  # pragma: no cover - the function either returns a row or raises
        raise RuntimeError("create_membership returned no id")
    return row[0]


# ============================================================== invitations


def _invitation_from_row(row: tuple) -> Invitation:
    return Invitation(
        id=row[0],
        organisation_id=row[1],
        email=row[2],
        subject=row[3],
        status=InvitationStatus(row[4]),
        delivery_state=DeliveryState(row[5]),
        principal_id=row[6],
        invited_at=row[7],
        expires_at=row[8],
        notified_at=row[9],
        accepted_at=row[10],
        withdrawn_at=row[11],
        awaiting_operator=row[4] == InvitationStatus.PENDING.value
        and row[5] == DeliveryState.OPERATOR_PENDING.value,
    )


_INVITATION_COLUMNS = (
    "id, organisation_id, email, subject, status, delivery_state, principal_id, "
    "created_at, expires_at, notified_at, accepted_at, withdrawn_at"
)


def create_invitation(
    conn: psycopg.Connection,
    principal: Principal,
    *,
    organisation_id: UUID,
    email: str | None = None,
    subject: str | None = None,
    expires_at: dt.datetime | None = None,
) -> Invitation:
    """Record that somebody should be invited.

    NOTHING IS SENT. This function has no mailer parameter, no transport and no
    network call: the deliverable is a row an operator can act on, and
    ``awaiting_operator`` is True on it. A product that mailed the invitation
    would need a queue, a retry policy, an unsubscribe story and a bounce
    handler, and the card explicitly says the stand must not do that.

    ``email`` is optional because an invite may name a person by their provider
    subject alone. Absent stays absent: ``None`` means "we do not have an
    address for this person", which is a true and useful thing to know.
    """
    if email is None and subject is None:
        raise ValueError("an invitation needs an email address or a provider subject")
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT kb.create_invitation(%s, %s, %s, %s)",
            (organisation_id, email, subject, expires_at),
        )
        row = _first(cur)
        if row is None:  # pragma: no cover
            raise RuntimeError("create_invitation returned no id")
        invitation_id = row[0]
        cur.execute(
            sql.SQL("SELECT {} FROM kb.invitation WHERE id = %s").format(
                sql.SQL(_INVITATION_COLUMNS)
            ),
            (invitation_id,),
        )
        found = _first(cur)
    if found is None:  # pragma: no cover
        raise RuntimeError("invitation is invisible to the administrator who made it")
    return _invitation_from_row(found)


def list_invitations(
    conn: psycopg.Connection,
    principal: Principal,
    organisation_id: UUID,
    *,
    status: InvitationStatus | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Invitation]:
    """The operator queue.

    An administrator sees the queue; anybody else sees only the invitations they
    have already accepted, and an invitation addressed by email is nobody's
    until it is accepted — an address is not an identity, and a queue row is not
    an admission ticket.
    """
    if not 1 <= limit <= 200 or offset < 0:
        raise ValueError("limit must be between 1 and 200 and offset must not be negative")
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            sql.SQL(
                "SELECT {} FROM kb.invitation "
                "WHERE organisation_id = %s AND (%s::text IS NULL OR status = %s::text) "
                "ORDER BY created_at, id LIMIT %s OFFSET %s"
            ).format(sql.SQL(_INVITATION_COLUMNS)),
            (organisation_id, status, status, limit, offset),
        )
        return [_invitation_from_row(r) for r in cur.fetchall()]


def mark_invitation_notified(
    conn: psycopg.Connection, principal: Principal, invitation_id: UUID
) -> Invitation:
    """Record that an operator told the person, out of band.

    This is the only way ``delivery_state`` leaves ``operator_pending``, and the
    database requires a named notifier. The stand has no mailer, so this
    records a human action rather than performing one.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute("SELECT kb.mark_invitation_notified(%s)", (invitation_id,))
        if _first(cur) is None:  # pragma: no cover
            raise RuntimeError("mark_invitation_notified returned nothing")
        cur.execute(
            sql.SQL("SELECT {} FROM kb.invitation WHERE id = %s").format(
                sql.SQL(_INVITATION_COLUMNS)
            ),
            (invitation_id,),
        )
        row = _first(cur)
    if row is None:  # pragma: no cover
        raise RuntimeError("invitation disappeared after being notified")
    return _invitation_from_row(row)


def withdraw_invitation(
    conn: psycopg.Connection, principal: Principal, invitation_id: UUID
) -> None:
    """Drop a pending invitation. Absence is an error, not a success."""
    with identity_cursor(conn, principal) as cur:
        cur.execute("SELECT kb.withdraw_invitation(%s)", (invitation_id,))
        if _first(cur) is None:  # pragma: no cover
            raise RuntimeError("withdraw_invitation returned nothing")


def accept_invitation(
    conn: psycopg.Connection,
    principal: Principal,
    invitation_id: UUID,
    *,
    issuer: str,
    subject: str,
) -> UUID:
    """Turn a pending invitation into a membership — for the caller only.

    ``issuer`` and ``subject`` are read from the verified token by the
    transport, never from the request body: the database forces the principal to
    ``current_principal()`` anyway, and it refuses an invitation nobody may see.
    A deactivated principal who is invited again and accepts becomes active
    again, and the journal says so twice.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute("SELECT kb.accept_invitation(%s, %s, %s)", (invitation_id, issuer, subject))
        row = _first(cur)
    if row is None:  # pragma: no cover
        raise RuntimeError("accept_invitation returned no membership id")
    return row[0]


# =================================================================== groups


def list_groups(
    conn: psycopg.Connection, principal: Principal, organisation_id: UUID
) -> list[AccessGroup]:
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT id, organisation_id, name, created_at FROM kb.access_group "
            "WHERE organisation_id = %s ORDER BY name",
            (organisation_id,),
        )
        return [
            AccessGroup(id=r[0], organisation_id=r[1], name=r[2], created_at=r[3])
            for r in cur.fetchall()
        ]


def create_group(
    conn: psycopg.Connection, principal: Principal, *, organisation_id: UUID, name: str
) -> AccessGroup:
    """Create a group. The journal entry is written by a database trigger.

    Not by this function: a trigger cannot be forgotten by the next caller, and
    a group change that reached the table without a journal line would be an
    access change nobody can account for.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "INSERT INTO kb.access_group (organisation_id, name, created_by) "
            "VALUES (%s, %s, %s) RETURNING id, organisation_id, name, created_at",
            (organisation_id, name, principal.principal_id),
        )
        row = _first(cur)
    if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
        raise AccessDenied("forbidden")
    return AccessGroup(id=row[0], organisation_id=row[1], name=row[2], created_at=row[3])


def add_group_member(
    conn: psycopg.Connection, principal: Principal, *, group_id: UUID, who: UUID
) -> GroupMember:
    """Add somebody to a group. Their grants through it change with this row."""
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "INSERT INTO kb.access_group_member "
            "       (group_id, organisation_id, principal_id, added_by) "
            "VALUES (%s, (SELECT organisation_id FROM kb.access_group WHERE id = %s), %s, %s) "
            "RETURNING group_id, principal_id, added_at",
            (group_id, group_id, who, principal.principal_id),
        )
        row = _first(cur)
    if row is None:  # pragma: no cover
        raise AccessDenied("forbidden")
    return GroupMember(group_id=row[0], principal_id=row[1], added_at=row[2])


def remove_group_member(
    conn: psycopg.Connection, principal: Principal, *, group_id: UUID, who: UUID
) -> None:
    """Take somebody out of a group.

    "No error" is not "it happened": an UPDATE or DELETE that RLS filtered
    reports a zero row count and raises nothing. A group removal reported as a
    success while the membership row survived would leave the person holding
    access they believe they lost, and the UI would then explain the wrong
    remaining path.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "DELETE FROM kb.access_group_member WHERE group_id = %s AND principal_id = %s",
            (group_id, who),
        )
        removed = cur.rowcount
    if removed == 0:
        raise AccessDenied("no such group member, or you may not manage it")


def set_group_grant(
    conn: psycopg.Connection,
    principal: Principal,
    *,
    group_id: UUID,
    library_id: UUID,
    role: str,
) -> GroupGrant:
    """Give a group a role on a library.

    ``role`` is the decision being made, not a claim about the caller: an
    administrator says which role a group gets. Who is allowed to say that is
    the RLS policy's question, and it is asked under the caller's own identity.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "INSERT INTO kb.access_group_grant (group_id, organisation_id, library_id, role) "
            "VALUES (%s, (SELECT organisation_id FROM kb.access_group WHERE id = %s), %s, %s) "
            "ON CONFLICT (group_id, library_id) "
            "DO UPDATE SET role = EXCLUDED.role, granted_at = now() "
            "RETURNING group_id, library_id, role::text, granted_at",
            (group_id, group_id, library_id, role),
        )
        row = _first(cur)
    if row is None:  # pragma: no cover
        raise AccessDenied("forbidden")
    return GroupGrant(group_id=row[0], library_id=row[1], role=row[2], granted_at=row[3])


def revoke_group_grant(
    conn: psycopg.Connection, principal: Principal, *, group_id: UUID, library_id: UUID
) -> None:
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "DELETE FROM kb.access_group_grant WHERE group_id = %s AND library_id = %s",
            (group_id, library_id),
        )
        removed = cur.rowcount
    if removed == 0:
        raise AccessDenied("no such group grant, or you may not manage it")


def list_group_grants(
    conn: psycopg.Connection, principal: Principal, group_id: UUID
) -> list[GroupGrant]:
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT group_id, library_id, role::text, granted_at FROM kb.access_group_grant "
            "WHERE group_id = %s ORDER BY library_id",
            (group_id,),
        )
        return [
            GroupGrant(group_id=r[0], library_id=r[1], role=r[2], granted_at=r[3])
            for r in cur.fetchall()
        ]


# =============================================== A08: which path is holding


def explain_library_access(
    conn: psycopg.Connection,
    principal: Principal,
    *,
    organisation_id: UUID,
    subject: UUID,
    library_id: UUID,
) -> AccessExplanation:
    """Say which grants are holding, one by one.

    ACCESS-MODEL A08: remove somebody from a group, then check the direct
    grant that is left; the UI explains the path that is still holding. After
    somebody is removed from a group, the direct grant is still real and the
    person still has access. Reporting a bare denial would be false; reporting
    "you may read" without saying through what would make the next removal
    look ineffective.

    The caller must be ``subject``, or an administrator of the organisation
    who ALREADY holds a role on this library. The database enforces the same
    rule independently, so a bug in this check discloses nothing: an
    administrator with no grant gets no rows from
    ``kb.effective_role_paths`` and is told ``not_visible`` rather than being
    shown an empty grant list, which would map every private library in the
    installation.
    """
    if principal.principal_id != subject:
        if not is_org_admin(conn, principal, organisation_id):
            raise AccessDenied("forbidden")
        if not holds_a_role(conn, principal, library_id):
            return AccessExplanation(
                library_id=library_id,
                principal_id=subject,
                effective_role=None,
                paths=[],
                denied=True,
                reason=DeniedBecause.NOT_VISIBLE,
                policy_revision=current_policy_revision(conn),
            )

    blocked = block_state(conn, principal, target=subject)

    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT path_kind, path_role::text, path_group, path_group_name "
            "  FROM kb.effective_role_paths(%s, %s) "
            " ORDER BY path_kind, path_group_name",
            (subject, library_id),
        )
        paths = [
            AccessPath(
                kind=AccessPathKind(r[0]),
                role=r[1],
                group_id=r[2],
                group_name=r[3],
            )
            for r in cur.fetchall()
        ]
        cur.execute("SELECT kb.current_policy_revision()")
        revision_row = _first(cur)
        revision = int(revision_row[0]) if revision_row else 0

    if blocked.blocked:
        # A closed membership ends every path, whatever the rows still say. The
        # paths are still returned — telling somebody which grants they used to
        # hold is the only way they can understand what they lost — but the
        # reason is the membership, not the absence of a grant.
        return AccessExplanation(
            library_id=library_id,
            principal_id=subject,
            effective_role=None,
            paths=paths,
            denied=True,
            reason=DeniedBecause.MEMBERSHIP_CLOSED,
            policy_revision=revision,
        )

    effective = max(paths, key=lambda p: _role_rank(p.role)).role if paths else None
    return AccessExplanation(
        library_id=library_id,
        principal_id=subject,
        effective_role=effective,
        paths=paths,
        denied=not paths,
        reason=DeniedBecause.NO_PATH,
        policy_revision=revision,
    )


def _role_rank(role: str) -> int:
    """Same order as ``kb.role_rank`` in SQL.

    Duplicated rather than fetched, because the ranking is four constants and a
    round trip per explanation would be a poor trade. The test suite asserts
    that this and the SQL function agree, so a change in one is caught.
    """
    return {"reader": 10, "contributor": 20, "curator": 30, "manager": 40}.get(role, 0)


def holds_a_role(conn: psycopg.Connection, principal: Principal, library_id: UUID) -> bool:
    """Does this principal have any role at all on this library?

    The one question the reporting path is allowed to ask about a library the
    caller is not the subject of. It is asked through ``kb.effective_role``,
    which is the same authority every policy uses, so an explanation and a row
    can never disagree about whether the caller may see the library.
    """
    with identity_cursor(conn, principal) as cur:
        cur.execute("SELECT kb.effective_role(kb.current_principal(), %s)", (library_id,))
        row = _first(cur)
    return bool(row and row[0])


# ================================================================= journal


def policy_journal(
    conn: psycopg.Connection,
    principal: Principal,
    organisation_id: UUID,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[JournalEntry]:
    """The audit trail, newest first.

    Readable by an administrator of the organisation and by the principal an
    entry is about. There is no write path from the product: the journal is
    appended by the SECURITY DEFINER functions and by a trigger, and kb_app
    holds no INSERT on it.
    """
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be between 1 and 500 and offset must not be negative")
    with identity_cursor(conn, principal) as cur:
        cur.execute(
            "SELECT revision, action, occurred_at, subject_principal_id, actor_principal_id, "
            "       library_id, group_id, invitation_id, detail "
            "  FROM kb.access_policy_journal "
            " WHERE organisation_id = %s "
            " ORDER BY occurred_at DESC, id DESC LIMIT %s OFFSET %s",
            (organisation_id, limit, offset),
        )
        return [
            JournalEntry(
                revision=r[0],
                action=r[1],
                occurred_at=r[2],
                subject_principal_id=r[3],
                actor_principal_id=r[4],
                library_id=r[5],
                group_id=r[6],
                invitation_id=r[7],
                detail=r[8] or {},
            )
            for r in cur.fetchall()
        ]


def is_org_admin(conn: psycopg.Connection, principal: Principal, organisation_id: UUID) -> bool:
    """Read-only, for the HTTP layer's 403 decisions. The SQL function decides."""
    with identity_cursor(conn, principal) as cur:
        cur.execute("SELECT kb.is_org_admin(%s, kb.current_principal())", (organisation_id,))
        row = _first(cur)
    return bool(row[0]) if row else False
