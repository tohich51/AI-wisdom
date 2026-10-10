"""C08 — an organisation administrator gets no content by being an administrator.

Acceptance case 3: "Администратор не получает содержание личной библиотеки
молча" — the administrator does not silently receive the content of somebody
else's private library.

"Молча" is the operative word. The dangerous version of this bug is not a
visible 403; it is a listing that quietly includes a private library, or a
count that includes it, or an admin panel that renders the title. So this file
asserts on the rows, on the counts, and on the API bodies — and it asserts the
positive control too, because a test that only proves denial also passes when
the administrator is simply broken.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def test_the_admin_set_table_carries_no_library_role(organisation, world, people, run_sql):
    """Read the column list: there is nowhere for a role to hide.

    ``kb.organisation_admin`` is two columns and a timestamp. A future column
    that widened a library grant would have to be added to this table, and this
    assertion would fail the day it was.
    """
    columns = world.conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'kb' AND table_name = 'organisation_admin' "
        "ORDER BY ordinal_position"
    ).fetchall()
    names = [row[0] for row in columns]
    assert names == ["organisation_id", "principal_id", "granted_at", "granted_by"], names
    assert not any("role" in n or "library" in n for n in names), names


def test_an_admin_sees_no_row_of_a_private_library(organisation, world, people, run_sql):
    """No grant, no row, in either content table."""
    owner = people.colleague
    lib = world.library(name="private-notes")
    world.source(lib, "canary-private")
    world.grant(lib, owner, "manager")
    world.membership(owner)

    rc, out = run_sql("SELECT title FROM kb.source", (), principal=people.admin)
    assert rc == 0 and "canary-private" not in out, out
    rc, out = run_sql("SELECT name FROM kb.library", (), principal=people.admin)
    assert "private-notes" not in out, out


def test_an_admin_cannot_write_into_a_private_library(organisation, world, people, run_sql):
    owner = people.colleague
    lib = world.library()
    world.source(lib, "canary-write")
    world.grant(lib, owner, "manager")
    world.membership(owner)

    rc, out = run_sql(
        "INSERT INTO kb.source (library_id, title, media_type, submitted_by, object_key, "
        "content_hash) VALUES (%s, 'injected', 'text/plain', %s, 'c08/admin', %s)",
        (lib, people.admin, "d" * 64),
        principal=people.admin,
    )
    assert rc != 0, "an organisation admin inserted into somebody else's library"
    assert "row-level security" in out.lower(), out


def test_an_admin_cannot_grant_themselves_access(organisation, world, people, run_sql):
    """Administration is not a key to the library catalogue."""
    owner = people.colleague
    lib = world.library()
    world.grant(lib, owner, "manager")
    world.membership(owner)

    rc, out = run_sql(
        "INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES (%s, %s, 'manager')",
        (lib, people.admin),
        principal=people.admin,
    )
    assert rc != 0
    assert "row-level security" in out.lower(), out
    assert world.count("SELECT count(*) FROM kb.library_grant WHERE library_id = %s", (lib,)) == 1


def test_an_admin_cannot_enter_a_group_that_grants_access(organisation, world, people, run_sql):
    """Joining a group is a grant, so the admin may not do it either."""
    owner = people.colleague
    lib = world.library()
    group = world.group("writers")
    world.group_member(group, owner)
    world.group_grant(group, lib, "curator")
    world.membership(owner)

    _rc, refusal = run_sql(
        "INSERT INTO kb.access_group_member (group_id, organisation_id, principal_id) "
        "VALUES (%s, %s, %s)",
        (group, organisation, people.admin),
        principal=people.admin,
    )
    assert "row-level security" in refusal.lower(), (
        "an administrator enrolled itself in a group that grants access: " + refusal
    )
    _rc, seen = run_sql("SELECT count(*) FROM kb.source", (), principal=people.admin)
    assert seen.strip() == "0", seen


def test_an_admin_still_manages_membership_and_invitations(organisation, world, people, run_sql):
    """The positive control.

    Without this, every test above would also pass for a principal whose
    administration had been broken entirely, which would prove nothing about
    the boundary.
    """
    who = uuid.uuid4()
    rc, out = run_sql(
        "SELECT kb.create_membership(%s, %s, 'iss', 'sub')",
        (organisation, who),
        principal=people.admin,
    )
    assert rc == 0, out
    assert world.membership_row(who) is not None

    rc, out = run_sql(
        "SELECT kb.create_invitation(%s, 'colleague@example.invalid', NULL)",
        (organisation,),
        principal=people.admin,
    )
    assert rc == 0, out
    assert (
        world.count(
            "SELECT count(*) FROM kb.invitation WHERE organisation_id = %s", (organisation,)
        )
        == 1
    )


def test_the_admin_api_answers_without_describing_a_library_they_cannot_see(
    api, organisation, world, people
):
    """The API layer, not just the database.

    A 403 is the ideal answer; a 200 with the library absent is acceptable too.
    What is not acceptable is a body that mentions the library's name, id or a
    count that includes it — that is the "silently" in the acceptance case.
    """
    owner = people.colleague
    lib = world.library(name="private-contracts")
    world.source(lib, "canary-api")
    world.grant(lib, owner, "manager")
    world.membership(owner)

    admin = api.as_(people.admin)
    listing = admin.get(f"/orgs/{organisation}/members")
    assert listing.status_code == 200, listing.text
    assert str(owner) in {m["principal_id"] for m in _json(listing)}

    explanation = admin.get(f"/orgs/{organisation}/access/{owner}/libraries/{lib}")
    assert explanation.status_code == 200, explanation.text
    body = explanation.text
    assert "private-contracts" not in body, body
    assert "canary-api" not in body, body
    assert "manager" not in body, body
    payload = explanation.json()
    # "you may not look", not "they have no access": the administrator learns
    # nothing about a library they cannot open, not even that it has a holder
    assert payload["denied"] is True
    assert payload["paths"] == []
    assert payload["effective_role"] is None
    assert payload["reason"] == "not_visible"


def test_an_admin_who_manages_the_library_does_see_the_paths(api, organisation, world, people):
    """The positive control for the boundary above.

    An administrator who is a manager of the library is entitled to the grant
    list for it, and the explanation must work for them. Without this, the
    previous test would also pass for an endpoint that is simply broken.
    """
    owner = people.colleague
    lib = world.library(name="shared-plans")
    world.grant(lib, owner, "reader")
    world.grant(lib, people.admin, "manager")
    world.membership(owner)

    explanation = api.as_(people.admin).get(f"/orgs/{organisation}/access/{owner}/libraries/{lib}")
    assert explanation.status_code == 200, explanation.text
    payload = explanation.json()
    assert payload["denied"] is False
    assert payload["effective_role"] == "reader"
    assert [p["kind"] for p in payload["paths"]] == ["direct"]


def test_a_non_admin_sees_an_empty_queue_not_a_refusal(api, organisation, world, people):
    """The answer a member gets is "none", and it is not a 403.

    A 403 here would tell a colleague that an invitation queue exists and that
    they are not allowed to see it. Both facts are useful to them and neither is
    theirs. An empty list says exactly one thing: nothing for you.
    """
    world.membership(people.colleague)
    # seeded as the owner, standing in for an invitation an administrator raised
    world.conn.execute(
        "INSERT INTO kb.invitation (organisation_id, email, created_by) "
        "VALUES (%s, 'a@example.invalid', %s)",
        (organisation, people.admin),
    )
    caller = api.as_(people.colleague)
    queue = caller.get(f"/orgs/{organisation}/invitations")
    assert queue.status_code == 200, queue.text
    assert _json(queue) == []

    members = caller.get(f"/orgs/{organisation}/members")
    assert members.status_code == 200
    assert {m["principal_id"] for m in _json(members)} == {str(people.colleague)}


def test_the_last_administrator_cannot_be_removed(organisation, world, people, run_sql):
    """An organisation with no administrator has no supported way back.

    The only way back would be a direct psql session, which is the operational
    hole this card exists to avoid.
    """
    rc, out = run_sql(
        "SELECT kb.remove_organisation_admin(%s, %s)",
        (organisation, people.admin),
        principal=people.admin,
    )
    assert rc != 0
    assert "last administrator" in out, out
    assert (
        world.count(
            "SELECT count(*) FROM kb.organisation_admin WHERE organisation_id = %s",
            (organisation,),
        )
        == 1
    )


def test_a_second_administrator_can_be_appointed_and_can_remove_the_first(
    organisation, world, people, run_sql
):
    rc, out = run_sql(
        "SELECT kb.add_organisation_admin(%s, %s)",
        (organisation, people.stranger),
        principal=people.admin,
    )
    assert rc == 0, out
    rc, out = run_sql(
        "SELECT kb.remove_organisation_admin(%s, %s)",
        (organisation, people.admin),
        principal=people.stranger,
    )
    assert rc == 0, out
    assert (
        world.count(
            "SELECT count(*) FROM kb.organisation_admin WHERE organisation_id = %s",
            (organisation,),
        )
        == 1
    )


def test_the_policy_journal_is_administrative_and_append_only(organisation, world, people, run_sql):
    """Readable by the admin, writable by nobody in the product."""
    who = uuid.uuid4()
    rc, out = run_sql(
        "SELECT kb.create_membership(%s, %s, 'iss', 'sub')",
        (organisation, who),
        principal=people.admin,
    )
    assert rc == 0, out

    rc, seen = run_sql(
        "SELECT action FROM kb.access_policy_journal WHERE organisation_id = %s",
        (organisation,),
        principal=people.admin,
    )
    assert "membership_created" in seen, seen

    for statement in (
        "UPDATE kb.access_policy_journal SET action = 'membership_created'",
        "DELETE FROM kb.access_policy_journal",
    ):
        rc, out = run_sql(statement, (), principal=people.admin)
        assert rc != 0, (statement, out)
        assert "permission denied" in out, out


def _json(response):
    import json

    return json.loads(response.text)
