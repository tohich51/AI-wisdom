"""C08 — HTTP surface for membership, invitations, groups and the journal.

Where identity comes from
-------------------------

``current_principal`` is imported from :mod:`kb.http.libraries` (C09's router,
imported here rather than copied, because this card may not edit that file).
It reads ``request.state.principal``, which C07's authentication middleware
sets after it has verified the Keycloak token. There is no fallback: no header,
no query parameter, no cookie and no body field can become a principal.

``current_identity`` is the same story for ``(issuer, subject)``. Accepting an
invitation needs the provider coordinates of the *caller*, and they are read
from a verified :class:`kb.access.identity.VerifiedIdentity` on the request
state. It is refused with 401 when absent, so a request that somehow reached
this router without a verified token cannot create a membership.

What a request model may and may not carry
------------------------------------------

Every body here is ``extra="forbid"``, and none of them has a ``user_id``, an
``actor`` or a ``caller`` field. A ``principal_id`` in a path is the *subject*
of an administrator's action — "block this person" — and a ``role`` in a body
is the decision a grant confers, not a claim about who is asking. The
difference is the whole of rule 4, and a test asserts that a body carrying
``principal_id`` is rejected with 422 before a handler runs.

Deactivation
------------

``POST /orgs/{id}/members/{principal_id}/deactivate`` performs the ordered
sequence from :mod:`kb.access.membership_deactivation`: the membership closes in
PostgreSQL and commits, and only then is the identity provider contacted. The
revoker comes from ``app.state.session_revoker``. When it is absent the endpoint
returns 503 and changes nothing — it does NOT proceed with the PostgreSQL half
alone, because a caller that got a 503 and a member who was silently not
revoked is the worst of both. A deployment that wants the block to happen
anyway configures a revoker that reports its own failure; the block is never
conditional on the provider.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from kb.access import membership
from kb.access.identity import VerifiedIdentity
from kb.access.membership import MembershipInactive
from kb.access.membership_deactivation import (
    DeactivationResult,
    SessionRevoker,
    deactivate_member,
    outstanding_revocations,
    retry_outstanding_revocations,
)
from kb.contracts.enums import LibraryRole
from kb.http.libraries import Db, Me, current_principal, mapped_errors

router = APIRouter(tags=["members"])

# Re-exported so a test can assert the routers share one identity source rather
# than two implementations that happen to agree today.
__all__ = ["Db", "Me", "current_identity", "current_principal", "router"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def current_identity(request: Request) -> VerifiedIdentity:
    """The caller's verified provider identity, or nothing.

    ``(issuer, subject)`` is what an invitation is accepted against and what a
    deactivation names to the provider. It comes from the verified token and
    never from a body: a request that could name its own ``subject`` could
    accept somebody else's invitation.
    """
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no verified identity")
    if not isinstance(identity, VerifiedIdentity):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed identity")
    return identity


Identity = Annotated[VerifiedIdentity, Depends(current_identity)]


def session_revoker(request: Request) -> SessionRevoker:
    """The provider client, or 503.

    There is deliberately no default. A gateway that has not been given an
    identity provider client cannot revoke sessions, and pretending otherwise
    with a no-op would produce a deactivation report that says "revoked" when
    nothing was.
    """
    revoker = getattr(request.app.state, "session_revoker", None)
    if revoker is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no session revoker is configured; refusing to report a deactivation",
        )
    return revoker


Revoker = Annotated[SessionRevoker, Depends(session_revoker)]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------ bodies


class InviteMember(_Model):
    """Record an intention to invite somebody. Nothing is sent.

    Both fields are optional and at least one is required. A person may be
    invited by provider subject alone; ``email`` stays ``None`` then, which is a
    fact about what we know, not a gap to be filled with a placeholder.
    """

    email: str | None = Field(default=None, max_length=320)
    subject: str | None = Field(default=None, max_length=255)
    expires_at: dt.datetime | None = None


class AddMemberDirectly(_Model):
    """Put an existing, already-verified identity into the organisation.

    No ``principal_id``: the principal is the *subject* of the action, but the
    identity provider coordinates that identify them are facts the transport
    already verified, and inventing a second way to say "this is me" is exactly
    the hole rule 4 closes. The endpoint takes the caller's own verified
    identity, so this body is empty on purpose and exists only to make the
    no-extra-fields rule explicit.
    """

    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class DeactivateMember(_Model):
    """Why. Free text from an administrator, stored as a journal value.

    No ``actor``: the person doing it is the transport identity, and a body that
    could name a different one would make the journal unattributable.
    """

    reason: str | None = Field(default=None, max_length=500)


class CreateGroup(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")


class SetGroupGrant(_Model):
    """The role a group gets on a library.

    This is the decision, not a claim about the caller. Who may make it is the
    RLS policy's question, asked under the caller's own transaction identity.
    """

    role: LibraryRole


class AcceptInvitation(_Model):
    """Deliberately empty.

    Accepting uses the caller's verified ``(issuer, subject)`` from the
    transport. The model is still declared on the route so that a body carrying
    a ``principal_id`` is rejected with 422 before the handler runs: a field
    that is obviously wrong is worse than a field that is quietly ignored.
    """


class MembershipCreated(_Model):
    membership_id: UUID


@contextlib.contextmanager
def member_errors():
    """C08's refusals, mapped, then everything C09's ``mapped_errors`` maps.

    ``MembershipInactive`` is separated from ``AccessDenied`` on purpose.
    "Forbidden" hides whether the object exists; "your membership is closed"
    hides nothing, because the caller already knows it is about themselves, and
    the UI has to render it differently from a permission problem.
    """
    try:
        with mapped_errors():
            yield
    except MembershipInactive as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "membership_closed") from exc


# ------------------------------------------------------------------ members


@router.get("/orgs/{organisation_id}/members")
def list_members(organisation_id: UUID, conn: Db, me: Me) -> list[membership.Membership]:
    """Everybody the caller may see, and nobody else.

    RLS decides: a member sees their own row, an organisation administrator
    sees the organisation's, and anybody else gets an empty list rather than a
    roster. There is no organisation-wide count anywhere on this endpoint —
    a total that is not the caller's own list is a directory (A20).
    """
    with member_errors():
        return membership.list_memberships(conn, me, organisation_id)


@router.get("/orgs/{organisation_id}/me")
def read_me(organisation_id: UUID, conn: Db, me: Me) -> membership.MembershipBlock:
    """The caller's own membership state.

    This is the one endpoint a deactivated member must still be able to reach,
    because it is what tells them they were blocked, when, and by whom. It
    carries no library name, no content and no grant: only the state of the one
    membership that is theirs.
    """
    with member_errors():
        state = membership.block_state(conn, me, target=me.principal_id)
    return state


@router.get("/orgs/{organisation_id}/members/{principal_id}")
def read_member(
    organisation_id: UUID, principal_id: UUID, conn: Db, me: Me
) -> membership.Membership:
    with member_errors():
        found = membership.get_membership(conn, me, organisation_id, principal_id)
    if found is None:
        # Same answer as "does not exist", for the same reason as everywhere
        # else: the difference is a directory of who is in this installation.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such member")
    return found


@router.post("/orgs/{organisation_id}/members/me", status_code=status.HTTP_201_CREATED)
def join(
    organisation_id: UUID, body: AddMemberDirectly, conn: Db, me: Me, idy: Identity
) -> MembershipCreated:
    """Join using the caller's own verified identity.

    Two routes into a membership: accept an invitation (the product's normal
    way) or this one, which is for a person an administrator added by hand. It
    can only ever add the caller — the principal is taken from the transport and
    the database forces the actor to be an administrator.
    """
    with member_errors():
        new_id = membership.add_member(
            conn,
            me,
            organisation_id=organisation_id,
            who=me.principal_id,
            issuer=idy.issuer,
            subject=idy.subject,
            display_name=body.display_name,
            email=body.email,
        )
    return MembershipCreated(membership_id=new_id)


# -------------------------------------------------------------- deactivation


@router.post("/orgs/{organisation_id}/members/{principal_id}/deactivate")
def deactivate(
    organisation_id: UUID,
    principal_id: UUID,
    body: DeactivateMember,
    conn: Db,
    me: Me,
    revoker: Revoker,
) -> DeactivationResult:
    """Block a member, then revoke their provider sessions. In that order.

    The response is the whole story: ``membership_closed_at`` is the commit of
    the PostgreSQL step, ``provider_revocated`` is what the provider said after
    it, and ``revocation_outstanding`` is true when the second half did not
    happen. A member is refused from the first of those instants either way.
    """
    with member_errors():
        return deactivate_member(
            conn,
            me,
            organisation_id=organisation_id,
            target=principal_id,
            revoker=revoker,
            reason=body.reason,
            now=_utcnow(),
        )


@router.get("/orgs/{organisation_id}/outstanding-revocations")
def outstanding(organisation_id: UUID, conn: Db, me: Me) -> list[UUID]:
    """Members whose provider sessions were not confirmed revoked.

    The block stands for all of them. This is a work list, not a list of people
    who might still be inside.
    """
    with member_errors():
        return outstanding_revocations(conn, me, organisation_id)


@router.post("/orgs/{organisation_id}/outstanding-revocations/retry")
def retry_revocations(
    organisation_id: UUID, conn: Db, me: Me, revoker: Revoker
) -> list[DeactivationResult]:
    """Re-attempt the provider half only.

    Phase 1 is not repeated: the membership is already closed, and re-running
    the first half against a closed membership would fail in a way that reads
    like a new problem.
    """
    with member_errors():
        return retry_outstanding_revocations(
            conn, me, organisation_id=organisation_id, revoker=revoker
        )


# -------------------------------------------------------------- invitations


@router.post("/orgs/{organisation_id}/invitations", status_code=status.HTTP_201_CREATED)
def invite(organisation_id: UUID, body: InviteMember, conn: Db, me: Me) -> membership.Invitation:
    """Record that somebody should be invited.

    NOTHING IS SENT. The response says ``awaiting_operator: true`` and the row
    is the queue an operator works through: ``GET /orgs/{id}/invitations``.
    This endpoint has no mailer, no queue and no network call, and a body with
    an email address cannot make it acquire one.
    """
    with member_errors():
        return membership.create_invitation(
            conn,
            me,
            organisation_id=organisation_id,
            email=body.email,
            subject=body.subject,
            expires_at=body.expires_at,
        )


@router.get("/orgs/{organisation_id}/invitations")
def invitation_queue(
    organisation_id: UUID,
    conn: Db,
    me: Me,
    status_filter: Annotated[membership.InvitationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[membership.Invitation]:
    """The operator queue.

    An administrator sees the organisation's invitations. Anybody else sees only
    the ones they have already accepted — an address is not an identity, and a
    pending invitation is not an admission ticket.
    """
    with member_errors():
        return membership.list_invitations(
            conn, me, organisation_id, status=status_filter, limit=limit, offset=offset
        )


@router.post("/orgs/{organisation_id}/invitations/{invitation_id}/notify")
def notify(invitation_id: UUID, conn: Db, me: Me) -> membership.Invitation:
    """Record that an operator told the person, out of band.

    The only transition out of ``operator_pending``. The database requires a
    named notifier, so a "notified" row without a human behind it cannot exist.
    """
    with member_errors():
        return membership.mark_invitation_notified(conn, me, invitation_id)


@router.post("/orgs/{organisation_id}/invitations/{invitation_id}/withdraw", status_code=204)
def withdraw(invitation_id: UUID, conn: Db, me: Me) -> Response:
    with member_errors():
        membership.withdraw_invitation(conn, me, invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/orgs/{organisation_id}/invitations/{invitation_id}/accept", status_code=204)
def accept(
    organisation_id: UUID,
    invitation_id: UUID,
    body: AcceptInvitation,
    conn: Db,
    me: Me,
    idy: Identity,
) -> Response:
    """Accept, as yourself.

    The membership is created for the caller's own principal — the database
    forces it — and its ``(issuer, subject)`` are the verified ones from the
    transport. The body is accepted and must be empty: any field naming an
    identity here would be a way to accept somebody else's invitation, so the
    model forbids extras and the route rejects a body that carries one.
    """
    del body  # present only so that a non-empty body is refused above
    with member_errors():
        membership.accept_invitation(
            conn, me, invitation_id, issuer=idy.issuer, subject=idy.subject
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------- groups


@router.get("/orgs/{organisation_id}/groups")
def list_groups(organisation_id: UUID, conn: Db, me: Me) -> list[membership.AccessGroup]:
    with member_errors():
        return membership.list_groups(conn, me, organisation_id)


@router.post("/orgs/{organisation_id}/groups", status_code=status.HTTP_201_CREATED)
def create_group(
    organisation_id: UUID, body: CreateGroup, conn: Db, me: Me
) -> membership.AccessGroup:
    with member_errors():
        return membership.create_group(conn, me, organisation_id=organisation_id, name=body.name)


@router.put("/orgs/{organisation_id}/groups/{group_id}/members/{principal_id}")
def add_to_group(
    organisation_id: UUID,
    group_id: UUID,
    principal_id: UUID,
    conn: Db,
    me: Me,
) -> membership.GroupMember:
    """Add somebody to a group. Their access through it changes with this row."""
    with member_errors():
        return membership.add_group_member(conn, me, group_id=group_id, who=principal_id)


@router.delete("/orgs/{organisation_id}/groups/{group_id}/members/{principal_id}", status_code=204)
def remove_from_group(
    organisation_id: UUID, group_id: UUID, principal_id: UUID, conn: Db, me: Me
) -> Response:
    """Take somebody out of a group.

    Their direct grants are untouched, and that is the point: the person may
    still have access, and
    ``GET /orgs/{id}/access/{principal_id}/libraries/{library_id}`` is what says
    through what.
    """
    with member_errors():
        membership.remove_group_member(conn, me, group_id=group_id, who=principal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/orgs/{organisation_id}/groups/{group_id}/grants/{library_id}")
def set_group_grant(
    organisation_id: UUID,
    group_id: UUID,
    library_id: UUID,
    body: SetGroupGrant,
    conn: Db,
    me: Me,
) -> membership.GroupGrant:
    with member_errors():
        return membership.set_group_grant(
            conn, me, group_id=group_id, library_id=library_id, role=body.role.value
        )


@router.delete("/orgs/{organisation_id}/groups/{group_id}/grants/{library_id}", status_code=204)
def revoke_group_grant(
    organisation_id: UUID, group_id: UUID, library_id: UUID, conn: Db, me: Me
) -> Response:
    with member_errors():
        membership.revoke_group_grant(conn, me, group_id=group_id, library_id=library_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/orgs/{organisation_id}/groups/{group_id}/grants")
def list_group_grants(
    organisation_id: UUID, group_id: UUID, conn: Db, me: Me
) -> list[membership.GroupGrant]:
    with member_errors():
        return membership.list_group_grants(conn, me, group_id)


# -------------------------------------------------------------------- A08


@router.get("/orgs/{organisation_id}/access/{principal_id}/libraries/{library_id}")
def explain_access(
    organisation_id: UUID,
    principal_id: UUID,
    library_id: UUID,
    conn: Db,
    me: Me,
) -> membership.AccessExplanation:
    """Which grants are holding for this person on this library.

    The answer to A08, and the reason it is not a boolean: with no explicit
    deny in v1, removing somebody from a group leaves their direct grant intact
    and removing the direct grant leaves the group's. "You may read, because of
    your own grant; the team group no longer counts" is a true answer, and
    "forbidden" would be a false one.
    """
    with member_errors():
        return membership.explain_library_access(
            conn,
            me,
            organisation_id=organisation_id,
            subject=principal_id,
            library_id=library_id,
        )


# ------------------------------------------------------------------ journal


@router.get("/orgs/{organisation_id}/policy-journal")
def journal(
    organisation_id: UUID,
    conn: Db,
    me: Me,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[membership.JournalEntry]:
    """The policy journal, newest first.

    Append-only, and unreadable by anybody who is neither an administrator of
    the organisation nor the subject of the entries. There is no endpoint that
    writes to it: entries come from the SECURITY DEFINER functions and from a
    database trigger, and the runtime role holds no INSERT on the table.
    """
    with member_errors():
        return membership.policy_journal(conn, me, organisation_id, limit=limit, offset=offset)
