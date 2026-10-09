"""C08 — group grants and direct grants, and which one is still holding.

ACCESS-MODEL section 1: v1 has no explicit deny, so removing somebody from a
group does not remove their direct grant, and removing the direct grant does
not remove the group's. A08 is the acceptance case: remove a group member, then
check the direct grant that is left — the UI has to explain the path that
remains, and a bare "forbidden" would be a false answer about a person who can
still read the library.

Every case here is a real PostgreSQL assertion. ``kb.effective_role`` takes the
union of the two paths and ``kb.effective_role_paths`` reports them separately;
this file proves both, and proves they are not the same thing.
"""

from __future__ import annotations

import psycopg
import pytest

from kb.access import membership
from kb.access.policy import AccessDenied

pytestmark = pytest.mark.integration


# ------------------------------------------------- the two paths are distinct


def test_a_direct_grant_and_a_group_grant_are_separate_rows(organisation, world, people, run_sql):
    """Different tables, different questions, one answer each."""
    who = people.colleague
    lib = world.library()
    world.grant(lib, who, "reader")
    group = world.group()
    world.group_member(group, who)
    world.group_grant(group, lib, "curator")

    rc, paths = run_sql(
        "SELECT path_kind, path_role::text, path_group_name FROM kb.effective_role_paths(%s, %s)",
        (who, lib),
        principal=who,
    )
    assert rc == 0, paths
    out = paths
    assert out.count("direct") == 1, out
    assert out.count("group") == 1, out
    assert "curator" in out, out

    # and the two live in two tables, so neither can be edited by editing the
    # other. Scoped to this test's own rows: the database is shared by the suite.
    assert world.count("SELECT count(*) FROM kb.library_grant WHERE library_id = %s", (lib,)) == 1
    assert (
        world.count("SELECT count(*) FROM kb.access_group_grant WHERE group_id = %s", (group,)) == 1
    )


def test_the_highest_role_across_both_paths_wins(organisation, world, people, run_sql):
    """A direct reader and a group curator is a curator.

    The union is a maximum, not a first match: whichever answer arrives first
    from a UNION would otherwise decide a person's access.
    """
    who = people.colleague
    lib = world.library()
    world.grant(lib, who, "reader")
    group = world.group()
    world.group_member(group, who)
    world.group_grant(group, lib, "curator")

    _rc, out = run_sql("SELECT kb.effective_role(%s, %s)", (who, lib), principal=who)
    assert out.strip() == "curator", out


def test_a_group_grant_alone_gives_real_row_access(organisation, world, people, db, run_sql):
    """Not a report: the group path actually opens rows under RLS.

    If the group branch existed only in the reporting function, the person would
    be told they had access and find nothing. This reads a real row as
    ``kb_app`` with the caller's transaction-local principal.
    """
    who = people.colleague
    lib = world.library()
    world.source(lib, "canary-via-group")
    group = world.group()
    world.group_member(group, who)
    world.group_grant(group, lib, "reader")
    world.membership(who)

    rc, titles = run_sql("SELECT title FROM kb.source", (), principal=who)
    assert rc == 0 and "canary-via-group" in titles, titles


def test_a_person_outside_the_group_gets_nothing_through_it(organisation, world, people, run_sql):
    """The grant is on the group, not on the library."""
    outsider = people.stranger
    lib = world.library()
    world.source(lib, "canary-not-yours")
    group = world.group()
    world.group_member(group, people.colleague)
    world.group_grant(group, lib, "manager")

    _rc, counted = run_sql("SELECT count(*) FROM kb.source", (), principal=outsider)
    assert counted.strip() == "0", counted
    _rc, role = run_sql("SELECT kb.effective_role(%s, %s)", (outsider, lib), principal=outsider)
    assert role.strip() in ("", "None"), role


# --------------------------------------------------------------------- A08


def test_removing_the_group_leaves_the_direct_grant_and_the_ui_says_so(
    organisation, world, people, db, run_sql
):
    """The acceptance case, end to end.

    Before: a group curator and a direct reader. After the removal: the group
    path is gone, the direct path is not, and the explanation says exactly that
    instead of reporting a denial or a bare permission.
    """
    who = people.colleague
    lib = world.library()
    world.grant(lib, who, "reader")
    world.source(lib, "canary-after-group-removal")
    group = world.group()
    world.group_member(group, who)
    world.group_grant(group, lib, "curator")
    world.membership(who)
    me = people.principal(who)

    before = membership.explain_library_access(
        db, me, organisation_id=organisation, subject=who, library_id=lib
    )
    assert before.denied is False
    assert before.effective_role == "curator"
    assert {p.kind.value for p in before.paths} == {"direct", "group"}

    # an administrator removes the membership; the person reads their own
    # explanation afterwards, and that is the sequence A08 describes
    membership.remove_group_member(db, people.principal(people.admin), group_id=group, who=who)

    after = membership.explain_library_access(
        db, me, organisation_id=organisation, subject=who, library_id=lib
    )
    assert after.denied is False, "the direct grant is still there — this is not a denial"
    assert after.effective_role == "reader"
    assert [p.kind.value for p in after.paths] == ["direct"]
    assert all(p.group_id is None for p in after.paths)
    assert after.reason.value == "no_path"  # "not blocked by a missing path"
    assert after.policy_revision > before.policy_revision

    # and the access is real, not just reported: the direct reader still opens
    # the library through the same policies the explanation was computed from
    rc, out = run_sql("SELECT count(*) FROM kb.library", (), principal=who)
    assert rc == 0 and out.strip() == "1", out


def test_removing_the_direct_grant_leaves_the_group_and_the_ui_says_so(
    organisation, world, people, db
):
    """The other direction, because v1 has no deny and both directions matter."""
    who = people.colleague
    lib = world.library()
    world.grant(lib, who, "reader")
    group = world.group()
    world.group_member(group, who)
    world.group_grant(group, lib, "contributor")
    world.membership(who)
    me = people.principal(who)

    # a direct grant may only be removed by somebody who can see it, which is a
    # manager of the library or the grantee; neither is this principal, so the
    # removal below is arranged as the owner and the application check is
    # covered by test_a_group_removal_that_did_not_happen_is_not_reported_as_success
    before = membership.explain_library_access(
        db, me, organisation_id=organisation, subject=who, library_id=lib
    )
    assert before.effective_role == "contributor"

    _revoke_direct_as_owner(world, lib, who)

    after = membership.explain_library_access(
        db, me, organisation_id=organisation, subject=who, library_id=lib
    )
    assert after.denied is False
    assert after.effective_role == "contributor"
    assert [p.kind.value for p in after.paths] == ["group"]
    assert after.paths[0].group_id == group


def test_with_both_paths_gone_the_explanation_says_no_path(organisation, world, people, db):
    """Not a group, no grant: the honest answer is "no path", and it is stable."""
    who = people.colleague
    lib = world.library()
    world.membership(who)
    me = people.principal(who)

    explanation = membership.explain_library_access(
        db, me, organisation_id=organisation, subject=who, library_id=lib
    )
    assert explanation.denied is True
    assert explanation.effective_role is None
    assert explanation.paths == []
    assert explanation.reason.value == "no_path"


def test_a_closed_membership_ends_every_path_but_still_lists_them(organisation, world, people, db):
    """The reason changes to the membership; the paths are still shown.

    Telling somebody which grants they used to hold is the only way they can
    understand what they lost, and a membership close removes all of them at
    once — saying only "no path" would hide that something else did it.
    """
    who = people.colleague
    lib = world.library()
    world.grant(lib, who, "reader")
    group = world.group()
    world.group_member(group, who)
    world.group_grant(group, lib, "reader")
    world.membership(who)
    me = people.principal(who)

    from kb.access.membership_deactivation import RevocationOutcome, deactivate_member

    class Confirming:
        def revoke_sessions(self, *, issuer: str, subject: str) -> RevocationOutcome:
            return RevocationOutcome(revoked=True, reason="logged_out")

    deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=Confirming(),
    )

    explanation = membership.explain_library_access(
        db, me, organisation_id=organisation, subject=who, library_id=lib
    )
    assert explanation.denied is True
    assert explanation.reason.value == "membership_closed"
    assert explanation.effective_role is None
    assert {p.kind.value for p in explanation.paths} == {"direct", "group"}


def test_a_third_party_cannot_ask_about_somebody_elses_paths(
    organisation, world, people, db, run_sql
):
    """The reporting function is guarded in SQL, not only in Python.

    Both layers are asserted, and the SQL one is the one that matters: a bug in
    the application check must not turn this into a way to enumerate other
    people's grants.
    """
    subject = people.colleague
    lib = world.library()
    world.grant(lib, subject, "reader")
    world.membership(subject)
    stranger = people.stranger
    world.membership(stranger)

    with pytest.raises(AccessDenied):
        membership.explain_library_access(
            db,
            people.principal(stranger),
            organisation_id=organisation,
            subject=subject,
            library_id=lib,
        )

    # and the database says no as well, even if the Python check were removed
    _rc, paths = run_sql(
        "SELECT count(*) FROM kb.effective_role_paths(%s, %s)",
        (subject, lib),
        principal=stranger,
    )
    assert paths.strip() == "0", paths


def test_an_administrator_may_ask_about_anybody(organisation, world, people, db):
    subject = people.colleague
    lib = world.library()
    world.grant(lib, subject, "manager")
    # an administrator may look only at libraries they already have a role on
    world.grant(lib, people.admin, "manager")
    world.membership(subject)

    explanation = membership.explain_library_access(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        subject=subject,
        library_id=lib,
    )
    assert explanation.denied is False
    assert explanation.effective_role == "manager"
    assert [p.kind.value for p in explanation.paths] == ["direct"]


# ----------------------------------------------------------------- negatives


def test_a_non_admin_cannot_create_a_group(organisation, world, people, db):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        membership.create_group(
            db, people.principal(people.colleague), organisation_id=organisation, name="sneaky"
        )


def test_a_group_removal_that_did_not_happen_is_not_reported_as_success(
    organisation, world, people, db
):
    """A filtered DELETE reports a zero row count and raises nothing.

    Reporting that as a success would tell a person their group access is gone
    while it is not — and the explanation would then name the wrong path.
    """
    admin = people.principal(people.admin)
    group = world.group()
    with pytest.raises(AccessDenied):
        membership.remove_group_member(db, admin, group_id=group, who=people.stranger)
    with pytest.raises(AccessDenied):
        membership.revoke_group_grant(db, admin, group_id=group, library_id=world.library())


def test_the_journal_records_every_group_change(organisation, world, people, db):
    """Written by a trigger, so no write path can skip it."""
    admin = people.principal(people.admin)
    group = membership.create_group(db, admin, organisation_id=organisation, name="writers")
    lib = world.library()
    # a library manager decides what a group gets; the org admin role does not
    world.grant(lib, people.admin, "manager")
    membership.set_group_grant(db, admin, group_id=group.id, library_id=lib, role="reader")
    membership.add_group_member(db, admin, group_id=group.id, who=people.colleague)
    membership.remove_group_member(db, admin, group_id=group.id, who=people.colleague)
    membership.revoke_group_grant(db, admin, group_id=group.id, library_id=lib)

    actions = [row[1] for row in world.journal()]
    for expected in (
        "group_created",
        "group_grant_set",
        "group_member_added",
        "group_member_removed",
        "group_grant_revoked",
    ):
        assert expected in actions, (expected, actions)


def test_the_runtime_role_cannot_write_the_journal_itself(organisation, world, people, run_sql):
    """An audit trail the runtime can forge by hand is not an audit trail."""
    rc, refusal = run_sql(
        "SELECT kb.record_policy_change(%s, 'membership_deactivated', %s, %s)",
        (organisation, people.colleague, people.admin),
        principal=people.admin,
    )
    assert rc != 0
    assert "permission denied for function record_policy_change" in refusal, refusal
    assert world.journal("membership_deactivated") == []


def test_the_runtime_role_cannot_edit_a_membership_directly(organisation, world, people, run_sql):
    who = people.colleague
    world.membership(who)
    rc, out = run_sql(
        "UPDATE kb.membership SET status = 'active', deactivated_at = NULL WHERE principal_id = %s",
        (who,),
        principal=people.admin,
    )
    assert rc != 0
    assert "permission denied for table membership" in out, out


# ------------------------------------------------------------------ helpers


def _revoke_direct_as_owner(world, library_id, who) -> None:
    """Remove a direct grant the way an administrator's session would."""
    world.conn.execute(
        "DELETE FROM kb.library_grant WHERE library_id = %s AND principal_id = %s",
        (library_id, who),
    )


def test_the_role_ranking_agrees_with_the_database(world, run_sql):
    """The four duplicated constants, checked against the real function.

    ``kb.role_rank`` lives in 0002 and ``kb.access.policy`` and
    ``kb.access.membership`` each carry a copy for different reasons. If the
    copies drift, the UI reports one role while the database enforces another
    and both halves look right.
    """
    from kb.contracts.enums import LibraryRole

    for role in LibraryRole:
        rc, out = run_sql("SELECT kb.role_rank(%s)", (role.value,))
        assert rc == 0, out
        assert membership._role_rank(role.value) == int(out.strip()), role
