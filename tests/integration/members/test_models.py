"""C08 — invariants that need no database.

Deliberately NOT marked ``integration``, so they run inside ``just check`` as
well as the full gate: a model that can carry a caller-asserted identity, or a
vocabulary that has drifted from the one the CHECK constraints enforce, is
caught without starting a server.

The vocabulary tests read ``migrations/0004_membership.sql`` directly rather
than a running database, which is the only way they can run here — and it is
also the stronger form: they compare what the Python says against what the file
*says*, not against what a server accepted.
"""

from __future__ import annotations

import pathlib
import re

import pytest
from pydantic import BaseModel, ValidationError

from kb.access import membership as m
from kb.access.membership import (
    AccessExplanation,
    AccessPath,
    DeliveryState,
    InvitationStatus,
    Membership,
    MembershipStatus,
)
from kb.http import members as h

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[3] / "migrations" / "0004_membership.sql"
).read_text(encoding="utf-8")


# --------------------------------------------------------- identity, again


@pytest.mark.parametrize(
    "model",
    [
        h.InviteMember,
        h.AddMemberDirectly,
        h.DeactivateMember,
        h.CreateGroup,
        h.SetGroupGrant,
        h.AcceptInvitation,
    ],
)
def test_no_request_model_carries_a_caller_asserted_identity(model):
    """Rule 4, on the models rather than on the wire.

    A ``user_id``, ``actor`` or ``caller`` field is a way for a request to say
    who it is. ``principal_id`` in a *path* is the subject of an administrator's
    action and is legitimate; in a *body* it is not, and none of these has one.
    """
    assert model.model_config.get("extra") == "forbid", model
    forbidden = {
        "user_id",
        "actor",
        "actor_id",
        "actor_principal_id",
        "caller",
        "principal_id",
        "role_as",
        "is_admin",
        "groups",
        "permissions",
    }
    assert forbidden.isdisjoint(model.model_fields), (model, set(model.model_fields))


def test_every_c08_model_forbids_extra_fields():
    for model in (
        Membership,
        AccessPath,
        AccessExplanation,
        m.Invitation,
        m.JournalEntry,
        m.AccessGroup,
        m.GroupMember,
        m.GroupGrant,
        m.MembershipBlock,
    ):
        assert issubclass(model, BaseModel)
        assert model.model_config.get("extra") == "forbid", model


def test_an_extra_field_is_rejected_not_ignored():
    """Ignored would be worse than rejected: a body could carry an actor and be
    quietly discarded, and the next refactor would start honouring it."""
    with pytest.raises(ValidationError):
        h.DeactivateMember(reason="left", actor_principal_id="00000000-0000-0000-0000-000000000001")
    with pytest.raises(ValidationError):
        h.InviteMember(email="a@example.invalid", principal_id="x")


def test_the_routers_share_one_identity_dependency():
    """Not two implementations that happen to agree today.

    ``kb.http.members`` imports the dependency from ``kb.http.libraries``
    because this card may not edit that file; the test asserts the import is
    still there rather than a copy of it.
    """
    assert h.current_principal is not None
    assert h.Me is not None
    assert h.Db is not None
    source = m.__file__ or ""
    assert source
    from kb.http import libraries

    assert h.current_principal is libraries.current_principal


# ------------------------------------------------------------- vocabularies


def _table_body(table: str) -> str:
    """The CREATE TABLE block for one table, up to the next one."""
    start = MIGRATION.index(f"CREATE TABLE {table} (")
    end = MIGRATION.find("CREATE TABLE ", start + 1)
    return MIGRATION[start : end if end > 0 else len(MIGRATION)]


def _check_list(table: str, column: str) -> set[str]:
    """The values a CHECK constraint allows for one column of one table.

    Scoped to the table, because ``status`` means two different things here and
    matching the first one would compare a membership status against an
    invitation status and find that one of them is wrong.
    """
    match = re.search(
        rf"\b{column}\s+text[^;]*?CHECK\s*\(\s*{column}\s+IN\s*\((.*?)\)\s*\)",
        _table_body(table),
        re.S,
    )
    assert match, f"no CHECK for {table}.{column}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_membership_status_matches_the_database():
    assert {s.value for s in MembershipStatus} == _check_list("membership", "status")


def test_invitation_vocabulary_matches_the_database():
    assert {s.value for s in InvitationStatus} == _check_list("invitation", "status")
    assert {s.value for s in DeliveryState} == _check_list("invitation", "delivery_state")


def test_the_python_role_ranking_matches_the_shared_one():
    """Four constants duplicated across the boundary; the test is the tie.

    A mismatch would make the UI report one role while the database enforces
    another, which is the worst kind of bug in an access model: both halves
    look right. The comparison against the real ``kb.role_rank`` function needs a
    server and lives in
    tests/integration/members/test_groups_and_grants.py.
    """
    from kb.access.policy import ROLE_RANK
    from kb.contracts.enums import LibraryRole

    for role in LibraryRole:
        assert m._role_rank(role.value) == ROLE_RANK[role], role
    assert m._role_rank("nonexistent") == 0


def test_the_journal_action_vocabulary_is_closed():
    """Every action the code can emit is a value the CHECK allows."""
    allowed = _check_list("access_policy_journal", "action")
    for action in (
        "membership_created",
        "membership_deactivated",
        "group_created",
        "group_member_added",
        "group_member_removed",
        "group_grant_set",
        "group_grant_revoked",
        "invitation_created",
        "invitation_withdrawn",
        "invitation_notified",
        "invitation_accepted",
        "organisation_admin_granted",
        "organisation_admin_revoked",
        "sessions_revoked",
        "sessions_revocation_failed",
    ):
        assert action in allowed, action


# --------------------------------------------------------- null stays null


def test_unknown_stays_none():
    """Nothing in a response may fill a value nobody supplied.

    An email that was never given must serialise as ``null``. A UI that renders
    that as "unknown" is a UI making a statement about a person it knows nothing
    about.
    """
    membership = Membership(
        principal_id="11111111-1111-4111-8111-111111111111",
        organisation_id="22222222-2222-4222-8222-222222222222",
        issuer="iss",
        subject="sub",
        status=MembershipStatus.ACTIVE,
        invited_at="2026-01-01T00:00:00Z",
        activated_at="2026-01-01T00:00:00Z",
    )
    assert membership.email is None
    assert membership.display_name is None
    assert membership.deactivated_at is None
    assert membership.deactivation_reason is None
    assert "null" in membership.model_dump_json()


def test_an_explanation_with_no_path_is_denied_and_says_which_kind_of_no():
    explanation = AccessExplanation(
        library_id="11111111-1111-4111-8111-111111111111",
        principal_id="22222222-2222-4222-8222-222222222222",
        denied=True,
        reason=m.DeniedBecause.NOT_VISIBLE,
        policy_revision=3,
    )
    assert explanation.effective_role is None
    assert explanation.paths == []
    body = explanation.model_dump_json()
    assert '"not_visible"' in body
    assert "null" in body


def test_a_blocked_membership_carries_no_placeholder_text():
    block = m.MembershipBlock(principal_id="22222222-2222-4222-8222-222222222222", blocked=True)
    assert block.status is None
    assert block.deactivated_at is None
    assert "unknown" not in block.model_dump_json()
    assert "n/a" not in block.model_dump_json().lower()
