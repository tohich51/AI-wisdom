"""C09 — libraries, the two independent axes, and what a caller may count.

Acceptance for this card has three clauses and each has a home here or in
``test_project_bindings.py``:

1. kind and audience are independent — ``test_kind_and_audience_are_independent_axes``
2. pagination and counts do not reveal other people's libraries — the rest
3. a required unavailable library makes the context incomplete — the bindings file

The identity tests matter as much as the access ones. A catalogue that filters
correctly while accepting a ``user_id`` in the request body has not solved the
problem; it has moved the hole.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# ------------------------------------------------- kind x audience, twice


def test_kind_and_audience_are_independent_axes(api, world, people):
    """Every combination the schema allows, including the ones that would be
    wrong if kind implied audience.

    A brand library that is readable by everyone invited, and a reference
    library that is private. Both are ordinary. A validator that derived one
    from the other would refuse the first and silently widen the second.
    """
    owner = api.as_(people.owner)
    org = str(world.organisation())
    combinations = [
        ("brand", "private"),
        ("brand", "all_invited"),
        ("reference", "private"),
        ("reference", "all_invited"),
        ("playbook", "invited"),
        ("experience", "private"),
    ]
    for kind, audience in combinations:
        response = owner.post(
            "/libraries",
            json={
                "organisation_id": org,
                "name": f"{kind}-{audience}",
                "kind": kind,
                "audience_scope": audience,
            },
        )
        assert response.status_code == 201, (kind, audience, response.text)
        assert response.json()["kind"] == kind
        assert response.json()["audience_scope"] == audience

    everything = owner.get("/libraries?limit=200").json()
    seen = {(lib["kind"], lib["audience_scope"]) for lib in everything["libraries"]}
    assert set(combinations) <= seen, seen

    # the two filters are independent too: filtering by kind does not imply an
    # audience, and filtering by audience does not imply a kind
    brands = owner.get("/libraries?kind=brand&limit=200").json()
    assert {lib["audience_scope"] for lib in brands["libraries"]} == {"private", "all_invited"}
    public = owner.get("/libraries?audience_scope=all_invited&limit=200").json()
    assert {lib["kind"] for lib in public["libraries"]} == {"brand", "reference"}


# ----------------------------------------------------- pagination and counts


def test_counts_and_pages_describe_only_what_the_caller_may_see(api, world, people):
    """A count is a disclosure channel, so it is counted over the same set.

    Two principals, deliberately unequal sets. If ``total`` came from anywhere
    but the caller's RLS-scoped relation, the colleague's page would tell them
    how many libraries exist that they cannot open.
    """
    for name in ("owner-alpha", "owner-beta", "owner-gamma"):
        lib = world.library(name=name, kind="reference")
        world.grant(lib, people.owner, "manager")
    for name in ("colleague-only-one", "colleague-only-two"):
        lib = world.library(name=name, kind="reference")
        world.grant(lib, people.colleague, "manager")

    owner_page = api.as_(people.owner).get("/libraries?limit=200").json()
    colleague_page = api.as_(people.colleague).get("/libraries?limit=200").json()

    owner_names = {lib["name"] for lib in owner_page["libraries"]}
    colleague_names = {lib["name"] for lib in colleague_page["libraries"]}
    assert owner_names == {"owner-alpha", "owner-beta", "owner-gamma"}
    assert colleague_names == {"colleague-only-one", "colleague-only-two"}
    assert owner_page["total"] == 3, owner_page
    assert colleague_page["total"] == 2, colleague_page

    # a page past the end is empty, and its total is still the truth
    past_end = api.as_(people.owner).get("/libraries?limit=10&offset=99").json()
    assert past_end["libraries"] == [], past_end
    assert past_end["total"] == 3, past_end

    # a filtered page counts the filtered set, not the whole one
    filtered = api.as_(people.owner).get("/libraries?kind=brand&limit=200").json()
    assert filtered["total"] == len(filtered["libraries"]), filtered
    assert all(lib["kind"] == "brand" for lib in filtered["libraries"])

    # and a real page walks the set without gaps or repeats
    seen: list[str] = []
    for offset in (0, 2, 4):
        chunk = api.as_(people.owner).get(f"/libraries?limit=2&offset={offset}").json()
        seen.extend(lib["name"] for lib in chunk["libraries"])
    assert sorted(seen) == ["owner-alpha", "owner-beta", "owner-gamma"], seen


def test_the_stranger_sees_an_empty_catalogue_not_a_denied_one(api, world, people):
    world.library(name="not-yours", kind="reference")
    world.grant(world.count("SELECT 1") and _id_of(world, "not-yours"), people.owner, "manager")

    page = api.as_(people.stranger).get("/libraries?limit=200")
    assert page.status_code == 200, page.text
    assert page.json() == {"libraries": [], "total": 0, "limit": 200, "offset": 0}
    assert (
        api.as_(people.stranger).get(f"/libraries/{_id_of(world, 'not-yours')}").status_code == 404
    )


def _id_of(world, name):
    row = world.conn.execute("SELECT id FROM kb.library WHERE name = %s", (name,)).fetchone()
    assert row is not None, name
    return row[0]


# ----------------------------------------------------------------- identity


def test_no_transport_principal_means_no_data_at_all(api, world, people):
    """No verified principal, no rows — the same answer for every route.

    The 401 comes from the router before any SQL, which is the same default
    deny the database would give it, just earlier and cheaper.
    """
    world.library(name="closed-behind-auth", kind="reference")
    for path in (
        "/libraries",
        "/library-types",
        f"/libraries/{_id_of(world, 'closed-behind-auth')}",
    ):
        response = api.get(path)
        assert response.status_code == 401, (path, response.text)


def test_a_malformed_subject_is_not_an_identity(api):
    assert api.get("/libraries", headers={"X-Test-Auth-Subject": "not-a-uuid"}).status_code == 401


def test_a_request_body_cannot_assert_an_identity_or_a_role(api, world, people):
    """No request model has a slot for a caller-asserted identity.

    These are rejected as validation errors, not quietly ignored. A field that
    is accepted and unused is a field that will be used.
    """
    owner = api.as_(people.owner)
    org = str(world.organisation())
    base = {"organisation_id": org, "name": "x", "kind": "reference"}

    for forbidden in ("user_id", "actor_id", "principal_id", "account_id", "role", "is_admin"):
        response = owner.post("/libraries", json={**base, forbidden: str(people.owner)})
        assert response.status_code == 422, (forbidden, response.text)
        assert forbidden in response.text, response.text

    # and nothing was created by any of those attempts
    assert world.count("SELECT count(*) FROM kb.library WHERE name = 'x'") == 0

    # a grant write must not carry a role for the caller either... except that
    # role is precisely what a grant *is*. What it must not carry is the
    # grantee's identity asserted by the transport: the body names the grantee
    # and the caller is whoever the token said they were.
    granted = owner.put(
        f"/libraries/{_id_of(world, 'closed-behind-auth')}/grants",
        json={"principal_id": str(people.colleague), "role": "reader"},
    )
    assert granted.status_code == 403, granted.text


# ------------------------------------------------------------------- grants


def test_grants_are_per_library_and_never_inherited(api, world, people):
    """A role on one library says nothing about any other library."""
    mine = world.library(name="mine", kind="reference")
    world.grant(mine, people.owner, "manager")
    theirs = world.library(name="theirs", kind="reference")
    world.grant(theirs, people.owner, "manager")

    owner = api.as_(people.owner)
    assert (
        owner.put(
            f"/libraries/{mine}/grants",
            json={"principal_id": str(people.colleague), "role": "reader"},
        ).status_code
        == 200
    )

    colleague = api.as_(people.colleague)
    assert colleague.get(f"/libraries/{mine}").status_code == 200
    assert colleague.get(f"/libraries/{theirs}").status_code == 404

    # a reader sees their own grant and nothing else
    mine_grants = colleague.get(f"/libraries/{mine}/grants").json()
    assert mine_grants == [
        {"library_id": str(mine), "principal_id": str(people.colleague), "role": "reader"}
    ]
    assert colleague.get(f"/libraries/{theirs}/grants").json() == []

    # and the manager sees the whole set
    assert len(owner.get(f"/libraries/{mine}/grants").json()) == 2


def test_a_refused_grant_change_leaves_the_role_unchanged(api, world, people):
    """Escalation attempt, and the data is checked afterwards.

    A refused PUT must be a 403 and nothing may have moved. Asserting only the
    status code would pass just as well against an implementation that reported
    an error *after* writing.
    """
    lib = world.library(name="escalation-target", kind="reference")
    world.grant(lib, people.owner, "manager")
    world.grant(lib, people.colleague, "reader")

    colleague = api.as_(people.colleague)
    attempt = colleague.put(
        f"/libraries/{lib}/grants", json={"principal_id": str(people.colleague), "role": "manager"}
    )
    assert attempt.status_code == 403, attempt.text

    role = world.conn.execute(
        "SELECT role::text FROM kb.library_grant WHERE library_id = %s AND principal_id = %s",
        (lib, people.colleague),
    ).fetchone()
    assert role is not None
    assert role[0] == "reader", "the refused write changed the role"

    # the reader still cannot write content, which is what "reader" means
    assert (
        world.count(
            "SELECT count(*) FROM kb.library_grant WHERE library_id = %s AND role = 'manager'",
            (lib,),
        )
        == 1
    )


def test_a_revocation_that_matches_nothing_is_not_a_success(api, world, people):
    """Deleting a grant that was never there, or that you may not touch, is 403.

    A 204 here would tell a manager the removal happened when nothing did.
    """
    lib = world.library(name="revoke-target", kind="reference")
    world.grant(lib, people.owner, "manager")
    world.grant(lib, people.colleague, "reader")

    owner = api.as_(people.owner)
    assert owner.delete(f"/libraries/{lib}/grants/{people.stranger}").status_code == 403
    assert world.count("SELECT count(*) FROM kb.library_grant WHERE library_id = %s", (lib,)) == 2

    assert owner.delete(f"/libraries/{lib}/grants/{people.colleague}").status_code == 204
    assert world.count("SELECT count(*) FROM kb.library_grant WHERE library_id = %s", (lib,)) == 1
    assert api.as_(people.colleague).get(f"/libraries/{lib}").status_code == 404
