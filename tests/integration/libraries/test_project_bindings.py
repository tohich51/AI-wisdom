"""C09 — the central negative tests: a project binding is not an access right.

Every test in this file exists to assert a *denial*. A green suite that only
proves the happy path would be worth nothing for an access model, so the shape
of the assertions matters as much as their number.

Two rules from C06 shape what follows:

* An ``UPDATE`` under RLS is **filtered** (``UPDATE 0``, no error) while an
  ``INSERT`` is **rejected** by ``WITH CHECK``. So no test here concludes
  "denied" from the absence of an error. It reads the row back and checks that
  the data is unchanged.
* A denial must not show through the *shape* of a response either. The context
  assertions check that no name, uuid, kind or rule text belonging to the closed
  library appears anywhere in the serialised body.
"""

from __future__ import annotations

from uuid import UUID

import pytest

pytestmark = pytest.mark.integration

RULE_CANARY = "acme-brand-rule-title-canary"
BRAND_CANARY = "acme-brand-book"


@pytest.fixture
def bound(people, world):
    """A project that links a brand library the colleague has no grant on.

    The shape is A12 exactly: the colleague is a *curator* of the project — a
    strong role, enough to see and curate the project itself — and holds nothing
    at all on the brand library the project links.
    """
    brand = world.library(name=BRAND_CANARY, kind="brand", audience_scope="private")
    world.grant(brand, people.owner, "manager")
    brand_source = world.source(brand, "acme-brand-secret-source")

    project = world.library(name="apollo-project", kind="project", audience_scope="invited")
    world.grant(project, people.owner, "manager")
    world.grant(project, people.colleague, "curator")
    world.project(project, description="internal work for Acme")
    world.link(project, brand, is_required=True)

    brand_rule = world.rule(brand, title=RULE_CANARY)
    world.pin(project, brand_rule, 1, is_required=True)

    return {
        "brand": brand,
        "source": brand_source,
        "project": project,
        "rule": brand_rule,
    }


# ------------------------------------------------------------------- tests


def test_project_binding_grants_no_access_to_the_linked_library(bound, people, world, run_sql):
    """THE test. Being curator of the project must not open the brand library.

    Asserted three independent ways, because any one of them could pass for the
    wrong reason: the role lookup, the row visibility, and the grant table.
    """
    brand = bound["brand"]
    project = bound["project"]

    # the project itself is fully visible to the colleague
    rc, out = run_sql(
        "SELECT name FROM kb.library WHERE id = %s",
        role="kb_app",
        principal=people.colleague,
        params=(project,),
    )
    assert (rc, out.strip()) == (0, "apollo-project"), out

    # the linked library is not
    rc, out = run_sql(
        "SELECT count(*) FROM kb.library WHERE id = %s",
        role="kb_app",
        principal=people.colleague,
        params=(brand,),
    )
    assert (rc, out.strip()) == (0, "0"), out

    # the effective role on it is None — the single clearest statement of the
    # invariant there is
    rc, out = run_sql(
        "SELECT coalesce(kb.effective_role(kb.current_principal(), %s)::text, 'NONE')",
        role="kb_app",
        principal=people.colleague,
        params=(brand,),
    )
    assert (rc, out.strip()) == (0, "NONE"), out

    # and the binding created no grant: only the owner's own manager row exists
    assert (
        world.count("SELECT count(*) FROM kb.library_grant WHERE library_id = %s", (brand,)) == 1
    ), "the binding created a grant row on the target library"


def test_project_member_cannot_read_or_write_the_linked_content(bound, world, run_sql, api, people):
    """Not merely invisible: unwritable, and a failed write leaves the data alone."""
    brand = bound["brand"]
    source = bound["source"]

    # an INSERT is rejected outright, by WITH CHECK
    rc, out = run_sql(
        "INSERT INTO kb.source (library_id, title, media_type, submitted_by, object_key, "
        "content_hash) VALUES (%s, 'forged', 'text/plain', %s, %s, %s)",
        role="kb_app",
        principal=people.colleague,
        params=(brand, people.colleague, f"c09/{source.hex[:12]}", "b" * 64),
    )
    assert rc == 1, out
    assert "row-level security" in out.lower(), out
    assert world.count("SELECT count(*) FROM kb.source WHERE title = 'forged'") == 0

    # an UPDATE is only filtered, so silence would look like success. Read the
    # data back as the owner instead.
    rc, out = run_sql(
        "UPDATE kb.source SET title = 'hijacked' WHERE id = %s",
        role="kb_app",
        principal=people.colleague,
        params=(source,),
    )
    assert rc == 0, out

    assert (
        world.count(
            "SELECT count(*) FROM kb.source WHERE id = %s AND title = 'acme-brand-secret-source'",
            (source,),
        )
        == 1
    ), "the filtered UPDATE changed the row: a zero row count is not success"
    assert world.count("SELECT count(*) FROM kb.source WHERE title = 'hijacked'") == 0

    # and over HTTP the library is simply not there
    assert api.as_(people.colleague).get(f"/libraries/{brand}").status_code == 404
    assert api.as_(people.owner).get(f"/libraries/{brand}").status_code == 200


def test_project_manifest_reports_a_blocked_link_without_describing_it(bound, api, people):
    """A blocked requirement is disclosed; the closed library is not.

    This is the A20 half of the card. Both a silent context and a verbose one
    are wrong, and they fail in opposite directions: the member must learn that
    their context is incomplete, and must not learn what is behind it.
    """
    brand = bound["brand"]
    project = bound["project"]
    rule = bound["rule"]

    response = api.as_(people.colleague).get(f"/projects/{project}/context")
    assert response.status_code == 200, response.text
    text = response.text
    payload = response.json()

    assert payload["context_state"] == "blocked", text
    assert payload["complete"] is False, text
    assert payload["blocked_link_count"] == 1, text
    assert payload["blocked_pin_count"] == 1, text

    (link,) = payload["links"]
    assert link == {
        "is_required": True,
        "status": "blocked",
        "library_id": None,
        "library": None,
    }, text
    # the id, the name, the kind and the content of the closed library must not
    # appear anywhere in the bytes on the wire
    assert str(brand) not in text
    assert BRAND_CANARY not in text
    assert str(rule) not in text
    assert RULE_CANARY not in text

    (pin,) = payload["pins"]
    assert pin["status"] == "blocked", text
    assert pin["rule_id"] is None and pin["rule"] is None, text
    assert pin["version_no"] == 1, text


def test_the_owner_who_holds_the_grant_sees_the_same_context_completed(bound, api, people):
    """The control.

    Without it, "blocked" proves nothing — it could just as easily be a broken
    join that never resolves anything. The same route, the same fixture, a
    principal who holds the grant: it must come back complete.
    """
    response = api.as_(people.owner).get(f"/projects/{bound['project']}/context")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["context_state"] == "complete", response.text
    assert payload["complete"] is True, response.text
    assert payload["blocked_link_count"] == 0 and payload["blocked_pin_count"] == 0
    assert payload["links"][0]["library_id"] == str(bound["brand"]), response.text
    assert payload["links"][0]["library"]["name"] == BRAND_CANARY, response.text
    assert payload["links"][0]["library"]["kind"] == "brand", response.text
    assert payload["pins"][0]["rule_id"] == str(bound["rule"]), response.text
    assert payload["pins"][0]["rule"]["title"] == RULE_CANARY, response.text


def test_link_policies_never_consult_the_target_role(run_sql):
    """A structural guard, read from the live database's own catalogue.

    The behavioural tests above could be defeated by a change that keeps the
    same observable result for today's fixtures. This one asks PostgreSQL how it
    parsed the policies and fails if anyone ever writes a role lookup against the
    *target* of a link. The invariant is a property of the policy text, so the
    policy text is what gets checked.
    """
    rc, out = run_sql(
        "SELECT policyname, coalesce(qual, ''), coalesce(with_check, '') "
        "FROM pg_policies WHERE schemaname = 'kb' AND tablename = 'project_library_link'"
    )
    assert rc == 0, out
    assert out.strip(), "project_library_link has no policies at all"
    for policyname, qual, with_check in (line.split("\t") for line in out.strip().splitlines()):
        where = f"{qual} {with_check}"
        assert "effective_role" in where, f"{policyname} consults no role at all: {where}"
        assert "current_principal(), project_id)" in where, (
            f"{policyname} does not key on the project's role: {where}"
        )
        assert "current_principal(), library_id)" not in where, (
            f"{policyname} consults the role on the target library — a binding is not a "
            f"grant: {where}"
        )


def test_a_link_row_has_nowhere_to_put_a_principal(run_sql):
    """A future column would be the quiet way to smuggle access through a link."""
    rc, out = run_sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'kb' AND table_name = 'project_library_link'"
    )
    assert rc == 0, out
    columns = set(out.split())
    assert columns == {"project_id", "library_id", "is_required", "created_at"}, columns


def test_a_stranger_gets_nothing_at_all(bound, run_sql, api, people):
    """No role on the project, so not even the blocked-context answer."""
    rc, out = run_sql(
        "SELECT count(*) FROM kb.project_library_link", role="kb_app", principal=people.stranger
    )
    assert (rc, out.strip()) == (0, "0"), "a stranger can see a project manifest"

    assert api.as_(people.stranger).get(f"/projects/{bound['project']}/context").status_code == 404
    assert api.as_(people.stranger).get(f"/libraries/{bound['brand']}").status_code == 404


def test_only_the_grant_opens_the_library_not_the_link(people, world, api, run_sql):
    """The same library, before and after an explicit grant.

    The link is created first and changes nothing. The grant is added second
    and changes everything. That ordering is the whole claim.
    """
    brand = world.library(name="late-brand", kind="brand")
    world.grant(brand, people.owner, "manager")
    project = world.library(name="late-project", kind="project")
    world.grant(project, people.owner, "manager")
    world.project(project)

    owner = api.as_(people.owner)
    colleague = api.as_(people.colleague)

    assert (
        owner.post(f"/projects/{project}/libraries", json={"library_id": str(brand)}).status_code
        == 204
    )
    assert colleague.get(f"/libraries/{brand}").status_code == 404
    rc, out = run_sql(
        "SELECT count(*) FROM kb.library WHERE id = %s",
        role="kb_app",
        principal=people.colleague,
        params=(brand,),
    )
    assert (rc, out.strip()) == (0, "0"), out

    granted = owner.put(
        f"/libraries/{brand}/grants", json={"principal_id": str(people.colleague), "role": "reader"}
    )
    assert granted.status_code == 200, granted.text
    assert colleague.get(f"/libraries/{brand}").status_code == 200


def test_a_pooled_connection_carries_no_identity(db_pool, people, world):
    """A05-shaped, and cheap: the pool must not remember the last caller.

    Two principals share one pool. If the colleague's identity survived the
    release, the next checkout would answer as the colleague — which is the
    failure mode a connection pool turns into a security incident.
    """
    from kb.catalog.libraries import list_libraries

    for name, who in (
        ("pooled-owner-lib", people.owner),
        ("pooled-colleague-lib", people.colleague),
    ):
        lib = world.library(name=name, kind="reference")
        world.grant(lib, who, "manager")

    with db_pool.connection() as conn:
        page = list_libraries(conn, people.principal(people.colleague))
        assert [lib.name for lib in page.libraries] == ["pooled-colleague-lib"], page

    with db_pool.connection() as conn:
        # same pool, no identity at all: default deny, not "the colleague again"
        total = conn.execute("SELECT count(*) FROM kb.library").fetchone()
    assert int(total[0]) == 0, total

    with db_pool.connection() as conn:
        page = list_libraries(conn, people.principal(people.owner))
        assert "pooled-owner-lib" in [lib.name for lib in page.libraries], page


def test_project_and_library_ids_are_distinct_objects(bound):
    """Sanity, and a guard against a link that silently became a self-link."""
    assert isinstance(bound["project"], UUID)
    assert bound["project"] != bound["brand"]
    assert bound["source"] != bound["project"]
