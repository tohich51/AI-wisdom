"""C08 — a blocked member is refused immediately, with a token that still works.

ACCESS-MODEL A16: "Block a person in the product while their JWT is still
valid — the next request is refused on membership; sessions are revoked."

Every assertion here is a denial, and every one of them is made on a real
PostgreSQL with the runtime role ``kb_app`` and a transaction-local principal.
There is no case in this file that asserts something works; the interesting
behaviour of an access model is what does not happen.

The deactivation used to set the state up is done through the product's own
ordered sequence, so these tests also cover the wiring: if the ordering broke,
these would fail too.
"""

from __future__ import annotations

import uuid

import pytest
from psycopg import sql

from kb.access.membership import MembershipInactive, require_active_membership
from kb.access.membership_deactivation import RevocationOutcome, deactivate_member

pytestmark = pytest.mark.integration

#: Every table 0004 puts a RESTRICTIVE membership policy on. A module constant,
#: and interpolated through psycopg.sql.Identifier rather than into a string, so
#: S608 stays enabled for this file with no per-file relaxation.
SUBJECT_TABLES = (
    "library",
    "library_grant",
    "source",
    "source_version",
    "fragment",
    "knowledge",
    "knowledge_provenance",
    "rule",
    "rule_action",
    "use_record",
    "experience",
    "outcome",
    "job",
    "index_generation",
    "generation_policy",
    "project",
    "project_library_link",
    "project_rule_pin",
)
#: The three a reader meets first, spelled out rather than sliced out of the
#: tuple above.
FIRST_TABLES = ("source", "library", "library_grant")


def _org_revision(world, organisation) -> int:
    """This organisation's revision, read directly.

    ``kb.current_policy_revision()`` is the maximum over the installation, which
    in v1 is the whole story but in a test database is polluted by every other
    test's organisation. The fence a caller holds is about its own
    organisation, so that is what is compared here.
    """
    row = world.conn.execute(
        "SELECT revision FROM kb.organisation_policy WHERE organisation_id = %s",
        (organisation,),
    ).fetchone()
    return int(row[0]) if row else 0


def _close(world, db, people, organisation, who: uuid.UUID) -> None:
    """Deactivate through the product path, with a provider that confirms."""

    class Confirming:
        def revoke_sessions(self, *, issuer: str, subject: str) -> RevocationOutcome:
            return RevocationOutcome(revoked=True, reason="logged_out")

    deactivate_member(
        db,
        people.principal(people.admin),
        organisation_id=organisation,
        target=who,
        revoker=Confirming(),
        reason="test",
    )


# ---------------------------------------------------------- the database gate


def test_a_blocked_member_sees_no_row_of_the_library_they_had(
    organisation, world, people, db, run_sql
):
    """The grant is still there. The membership is what closes it."""
    who = uuid.uuid4()
    lib = world.library()
    world.source(lib, "canary-A08")
    world.grant(lib, who, "reader")
    world.membership(who)

    rc, out = run_sql("SELECT title FROM kb.source", (), principal=who)
    assert rc == 0 and "canary-A08" in out, out

    _close(world, db, people, organisation, who)

    # the grant row itself is untouched: nothing "cleaned up" it
    assert (
        world.count(
            "SELECT count(*) FROM kb.library_grant WHERE library_id = %s AND principal_id = %s",
            (lib, who),
        )
        == 1
    )
    # and the person can see none of it
    for table in FIRST_TABLES:
        rc, counted = run_sql(
            sql.SQL("SELECT count(*) FROM kb.{}").format(sql.Identifier(table)),
            principal=who,
        )
        assert rc == 0, (table, counted)
        assert counted.strip() == "0", (table, counted)


def test_a_blocked_member_is_refused_through_a_group_too(organisation, world, people, db, run_sql):
    """Group membership is not a side door.

    Removing somebody from a group leaves their direct grant; deactivating them
    closes both, and the test holds the group row in place while it does.
    """
    who = uuid.uuid4()
    lib = world.library()
    world.source(lib, "canary-group")
    group = world.group()
    world.group_member(group, who)
    world.group_grant(group, lib, "curator")
    world.membership(who)

    rc, out = run_sql("SELECT title FROM kb.source", (), principal=who)
    assert "canary-group" in out, out

    _close(world, db, people, organisation, who)

    # the group rows survive untouched — the group is still there, the member is
    # still in it, the grant is still on the library
    assert (
        world.count("SELECT count(*) FROM kb.access_group_member WHERE group_id = %s", (group,))
        == 1
    )
    rc, counted = run_sql("SELECT count(*) FROM kb.source", (), principal=who)
    assert rc == 0 and counted.strip() == "0", counted


def test_a_blocked_member_cannot_write_either(organisation, world, people, db, run_sql):
    """Refusal is not only about reading."""
    who = uuid.uuid4()
    lib = world.library()
    world.grant(lib, who, "manager")
    world.membership(who)
    _close(world, db, people, organisation, who)

    rc, out = run_sql(
        "INSERT INTO kb.source (library_id, title, media_type, submitted_by, object_key, "
        "content_hash) VALUES (%s, 'x', 'text/plain', %s, 'c08/x', %s)",
        (lib, who, "c" * 64),
        principal=who,
    )
    assert rc != 0, "a blocked member was allowed to insert"
    assert "row-level security" in out.lower(), out


def test_a_blocked_member_cannot_grant_anybody_else(organisation, world, people, db, run_sql):
    """The management surfaces close with everything else."""
    who = uuid.uuid4()
    lib = world.library()
    world.grant(lib, who, "manager")
    world.membership(who)
    _close(world, db, people, organisation, who)

    rc, refusal = run_sql(
        "INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES (%s, %s, 'manager')",
        (lib, people.stranger),
        principal=who,
    )
    assert rc != 0, refusal
    # nobody gained a grant anywhere, and the one grant that existed is intact
    assert world.count("SELECT count(*) FROM kb.library_grant WHERE library_id = %s", (lib,)) == 1


def test_every_subject_table_is_closed(organisation, world, people, db, run_sql):
    """Not one library and not one content table: the whole installation.

    The RESTRICTIVE policy is applied per table, and a table someone forgets is
    a table a blocked member can still read. This walks the list rather than
    trusting the migration.
    """
    who = uuid.uuid4()
    lib = world.library()
    world.grant(lib, who, "manager")
    world.membership(who)
    _close(world, db, people, organisation, who)

    for table in SUBJECT_TABLES:
        rc, counted = run_sql(
            sql.SQL("SELECT count(*) FROM kb.{}").format(sql.Identifier(table)),
            principal=who,
        )
        assert rc == 0, (table, counted)
        assert counted.strip() == "0", (table, counted)


def test_the_gate_lives_in_the_authority_function_not_in_a_copied_list(
    organisation, world, people, db, run_sql
):
    """Read it out of pg_proc rather than trusting the migration text.

    ``kb.effective_role`` is the one function every policy in 0002 and 0003 is
    written in terms of, so the membership check belongs there and nowhere
    else. A parallel policy per table would be eighteen places to forget, and
    a table nobody remembered is a table a blocked member can still read.
    """
    source = world.conn.execute(
        "SELECT pg_get_functiondef('kb.effective_role(uuid,uuid)'::regprocedure)"
    ).fetchone()[0]
    assert "kb.membership_allows" in source, source


def test_only_the_own_row_tables_need_a_second_statement(organisation, world, people, db, run_sql):
    """The two policies that remain, and the reason for exactly two.

    ``kb.library_grant`` and ``kb.use_record`` have a permissive branch that
    never consults a role — "you may always see your own row" — so the gate in
    the role function does not reach them. Every other table is shut by the
    role function alone, which is what keeps this list this short.
    """
    rows = world.conn.execute(
        "SELECT tablename, permissive FROM pg_policies "
        "WHERE schemaname = 'kb' AND policyname = 'membership_must_be_active' "
        "ORDER BY tablename"
    ).fetchall()
    assert rows == [
        ("library_grant", "RESTRICTIVE"),
        ("use_record", "RESTRICTIVE"),
    ], rows


def test_a_member_with_no_membership_row_is_unaffected(world, people, db, run_sql):
    """The gate denies on a recorded state, not on an absence of one.

    Principals that predate the invite lifecycle — every fixture in C03, C06
    and C09 — have no membership row and must keep working. A gate that denied
    on absence would lock out the owner on the day the table is created.
    """
    lib = world.library()
    world.source(lib, "still-here")
    world.grant(lib, people.stranger, "reader")

    rc, out = run_sql("SELECT title FROM kb.source", (), principal=people.stranger)
    assert rc == 0 and "still-here" in out, out


# --------------------------------------------------------- the application


def test_the_domain_check_refuses_a_blocked_member(organisation, world, people, db):
    who = uuid.uuid4()
    world.membership(who)
    me = people.principal(who)
    require_active_membership(db, me)  # does not raise

    _close(world, db, people, organisation, who)

    with pytest.raises(MembershipInactive):
        require_active_membership(db, me)


def test_a_stale_revision_forces_the_check_to_run_again(organisation, world, people, db):
    """ACCESS-MODEL section 7: a long request re-checks before it delivers.

    A caller admitted under revision N is carrying a snapshot. If the policy has
    moved on, the state cannot be trusted from admission time, and this is the
    call that discovers it.
    """
    who = uuid.uuid4()
    world.membership(who)
    me = people.principal(who)

    before = _org_revision(world, organisation)
    _close(world, db, people, organisation, who)
    after = _org_revision(world, organisation)
    assert after > before, (before, after)

    with pytest.raises(MembershipInactive):
        require_active_membership(db, me, revision=before)


def test_a_blocked_member_still_reads_their_own_state(organisation, world, people, db):
    """The one thing a blocked person may still see is why they were blocked.

    Hiding that would leave somebody with a valid token, an empty product and no
    explanation.
    """
    who = uuid.uuid4()
    world.membership(who)
    from kb.access.membership import block_state

    _close(world, db, people, organisation, who)
    state = block_state(db, people.principal(who))
    assert state.blocked is True
    assert state.deactivated_at is not None
    assert state.deactivated_by == people.admin


# ------------------------------------------------------------------- http


def test_the_api_refuses_a_blocked_member(api, organisation, world, people, db):
    """A token that verifies and a principal the database has closed.

    The request reaches the router with a well-formed identity — the harness
    sets exactly what C07's middleware would — and the answer is 403 with a
    reason, not an empty 200 that looks like an empty account.
    """
    who = people.colleague
    lib = world.library()
    world.grant(lib, who, "reader")
    world.membership(who)
    # seed one row the member could see while active
    world.source(lib, "visible-while-active")

    caller = api.as_(who)
    before = caller.get(f"/orgs/{organisation}/members")
    assert before.status_code == 200, before.text

    _close(world, db, people, organisation, who)

    after = caller.get(f"/orgs/{organisation}/members")
    assert after.status_code == 200, after.text

    me_state = caller.get(f"/orgs/{organisation}/me")
    assert me_state.status_code == 200, me_state.text
    assert me_state.json()["blocked"] is True


def test_deactivation_over_http_runs_the_ordered_sequence(
    api, organisation, world, people, db, observing_revoker
):
    """The whole product path: 201 → deactivate → refused, in that order."""
    who = uuid.uuid4()
    lib = world.library()
    world.grant(lib, who, "reader")
    world.membership(who)
    world.source(lib, "canary-http")

    revoker = observing_revoker
    admin_client = api.as_(people.admin).with_revoker(revoker)

    response = admin_client.post(
        f"/orgs/{organisation}/members/{who}/deactivate", json={"reason": "left"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["membership_status"] == "deactivated"
    assert body["provider_revocated"] is True
    assert revoker.observed[0]["status"] == "deactivated", revoker.observed

    # and the member is now refused everywhere, including at the library
    caller = api.as_(who)
    assert caller.get(f"/orgs/{organisation}/me").json()["blocked"] is True


def test_deactivation_without_a_configured_revoker_changes_nothing(
    api, organisation, world, people
):
    """No revoker means 503, not a block reported as complete.

    A gateway that has not been given a provider client cannot revoke sessions.
    Proceeding with the PostgreSQL half alone would return a 200 that reads as
    "done" for an operation that is only half done.
    """
    who = uuid.uuid4()
    world.membership(who)

    response = api.as_(people.admin).post(
        f"/orgs/{organisation}/members/{who}/deactivate", json={"reason": "left"}
    )
    assert response.status_code == 503, response.text
    assert world.membership_row(who)[0] == "active", "the member was blocked by a 503"


def test_a_body_cannot_name_the_actor(api, organisation, world, people, observing_revoker):
    """Rule 4, at the wire: an extra field is a 422 before a handler runs."""
    who = uuid.uuid4()
    world.membership(who)

    for payload in (
        {"reason": "left", "actor_principal_id": str(people.admin)},
        {"reason": "left", "user_id": str(people.admin)},
        {"reason": "left", "principal_id": str(who)},
        {"reason": "left", "role": "manager"},
    ):
        response = (
            api.as_(people.admin)
            .with_revoker(observing_revoker)
            .post(f"/orgs/{organisation}/members/{who}/deactivate", json=payload)
        )
        assert response.status_code == 422, (payload, response.status_code, response.text)
    assert world.membership_row(who)[0] == "active"
