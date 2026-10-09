"""C09 — project context assembly, and the three ways it goes wrong.

Acceptance clause 3 of the card: a required library the caller cannot open must
produce an *incomplete or blocked* context. Not a small one, and not one that
quietly omits the requirement.

The two failure modes are symmetric and both are wrong, which is why the tests
come in pairs: a context that withholds a mandatory brand rule and calls itself
complete is worse than an error, and a context that describes the closed library
to explain itself is a directory of invisible objects.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def org(world):
    return str(world.organisation())


def _make_library(api, org, people, name, kind="reference", audience="private"):
    response = api.as_(people.owner).post(
        "/libraries",
        json={
            "organisation_id": org,
            "name": name,
            "kind": kind,
            "audience_scope": audience,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_a_required_unavailable_library_makes_the_context_incomplete(api, world, org, people):
    """The acceptance case, end to end through HTTP.

    Built with the product's own API rather than seeded rows, so the test also
    covers the real creation path — including the bootstrap grant that 0002's
    policy could not express on its own.
    """
    owner = api.as_(people.owner)
    brand = _make_library(api, org, people, "closed-brand", kind="brand")
    project = owner.post(
        "/projects",
        json={
            "organisation_id": org,
            "name": "closed-project",
            "audience_scope": "invited",
            "description": "work that needs the brand",
        },
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project"]["id"]

    # the colleague is a curator of the project, and of nothing else
    assert (
        owner.put(
            f"/libraries/{project_id}/grants",
            json={"principal_id": str(people.colleague), "role": "curator"},
        ).status_code
        == 200
    )
    assert (
        owner.post(
            f"/projects/{project_id}/libraries", json={"library_id": brand, "is_required": True}
        ).status_code
        == 204
    )

    blocked = api.as_(people.colleague).get(f"/projects/{project_id}/context")
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["context_state"] == "blocked"
    assert blocked.json()["complete"] is False
    assert blocked.json()["blocked_link_count"] == 1
    assert blocked.json()["pins"] == []

    complete = owner.get(f"/projects/{project_id}/context")
    assert complete.status_code == 200, complete.text
    assert complete.json()["context_state"] == "complete"
    assert complete.json()["links"][0]["library"]["name"] == "closed-brand"


def test_an_optional_unavailable_link_is_still_reported_as_blocked(api, world, org, people):
    """Optional does not mean "pretend it is not there".

    The context is not the mandatory subset only — it is what this caller may
    use. A link they cannot open is unavailable whether or not it is required,
    and reporting it as available would be a lie in the other direction.
    """
    owner = api.as_(people.owner)
    closed = _make_library(api, org, people, "optional-closed")
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "optional-project"}
    ).json()["project"]["id"]
    owner.put(
        f"/libraries/{project_id}/grants",
        json={"principal_id": str(people.colleague), "role": "reader"},
    )
    assert (
        owner.post(
            f"/projects/{project_id}/libraries",
            json={"library_id": closed, "is_required": False},
        ).status_code
        == 204
    )

    response = api.as_(people.colleague).get(f"/projects/{project_id}/context")
    assert response.json()["context_state"] == "blocked", response.text
    (link,) = response.json()["links"]
    assert link["is_required"] is False
    assert link["status"] == "blocked"
    assert link["library_id"] is None and link["library"] is None


def test_a_pinned_rule_the_caller_cannot_read_blocks_the_context(api, world, org, people):
    """A brand rule is pinned, and its text is the thing most worth protecting."""
    owner = api.as_(people.owner)
    brand = _make_library(api, org, people, "pinned-brand", kind="brand")
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "pinned-project"}
    ).json()["project"]["id"]
    owner.put(
        f"/libraries/{project_id}/grants",
        json={"principal_id": str(people.colleague), "role": "curator"},
    )

    rule = world.rule(brand, title="never-show-this-title", actions=("never-show-this-step",))

    # the owner, who may read the brand library, pins it
    assert (
        owner.post(
            f"/projects/{project_id}/rules",
            json={"rule_id": str(rule), "version_no": 1, "is_required": True, "priority": 10},
        ).status_code
        == 204
    )

    response = api.as_(people.colleague).get(f"/projects/{project_id}/context")
    assert response.status_code == 200, response.text
    assert response.json()["context_state"] == "blocked", response.text
    assert response.json()["blocked_pin_count"] == 1
    (pin,) = response.json()["pins"]
    assert pin["status"] == "blocked"
    assert pin["version_no"] == 1, "the requirement is still disclosed as a version number"
    assert pin["is_required"] is True and pin["priority"] == 10
    assert pin["rule"] is None and pin["rule_id"] is None
    assert "never-show-this-title" not in response.text
    assert "never-show-this-step" not in response.text

    # the owner, who may read it, gets the rule in full
    full = owner.get(f"/projects/{project_id}/context").json()
    assert full["context_state"] == "complete"
    assert full["pins"][0]["rule"]["title"] == "never-show-this-title"
    assert full["pins"][0]["rule"]["actions"] == ["never-show-this-step"]


def test_the_mcp_result_carries_opaque_reasons_and_no_closed_names(api, world, org, people):
    """The MCP surface is remote, so a reason string is still a disclosure.

    ``get_project_context`` has to say something about unresolved dependencies;
    what it must not do is put a library name into a remote model's context.
    """
    from kb.access.policy import Principal
    from kb.catalog.projects import project_context

    owner = api.as_(people.owner)
    brand = _make_library(api, org, people, "mcp-closed-brand", kind="brand")
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "mcp-project"}
    ).json()["project"]["id"]
    owner.put(
        f"/libraries/{project_id}/grants",
        json={"principal_id": str(people.colleague), "role": "reader"},
    )
    owner.post(f"/projects/{project_id}/libraries", json={"library_id": brand})

    with db_of(api) as conn:
        ctx = project_context(
            conn,
            Principal(principal_id=people.colleague, account_id=people.account),
            project_id,
        )
    assert ctx is not None
    result = ctx.to_mcp_result()
    assert result.unresolved_dependencies == ["linked_library_access_missing"]
    assert result.pinned_rules == []
    serialised = result.model_dump_json()
    assert "mcp-closed-brand" not in serialised
    assert str(brand) not in serialised


def test_a_pin_must_name_a_version_the_rule_actually_has(api, world, org, people):
    """A pin is a version, and the database refuses to pretend otherwise.

    Two separate refusals, and they mean different things:

    * the composite foreign key on (rule_id, version_no) rejects a claim about a
      version the rule does not have, so "the current version" is not
      expressible;
    * the primary key on (project_id, rule_id) means a project pins exactly one
      version of a given rule, and moving to a corrected version is a
      deliberate update rather than a second mandate.

    Without the first, an experience recorded against a corrected rule becomes
    unattributable. Without the second, a project silently accumulates
    contradictory mandates.
    """
    owner = api.as_(people.owner)
    lib = _make_library(api, org, people, "versioned")
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "versioned-project"}
    ).json()["project"]["id"]
    rule = world.rule(lib, version_no=1, title="v1")

    def pinned() -> int:
        return world.count(
            "SELECT count(*) FROM kb.project_rule_pin WHERE project_id = %s", (project_id,)
        )

    wrong_version = owner.post(
        f"/projects/{project_id}/rules", json={"rule_id": str(rule), "version_no": 99}
    )
    assert wrong_version.status_code >= 400, wrong_version.text
    assert pinned() == 0, "the refused pin was written anyway"

    assert (
        owner.post(
            f"/projects/{project_id}/rules", json={"rule_id": str(rule), "version_no": 1}
        ).status_code
        == 204
    )
    assert pinned() == 1

    # the same rule cannot be pinned a second time under this project
    duplicate = owner.post(
        f"/projects/{project_id}/rules", json={"rule_id": str(rule), "version_no": 1}
    )
    assert duplicate.status_code >= 400, duplicate.text
    assert pinned() == 1, "a second mandate for the same rule was written anyway"


def test_a_pin_requires_curator_on_the_rule_library_too(api, world, org, people):
    """You cannot make a rule mandatory in a library you may not read.

    Otherwise a project member would be told a mandatory rule exists that no one
    on the project can ever resolve, and the context would be permanently
    blocked for a reason its own curators cannot fix.
    """
    owner = api.as_(people.owner)
    brand = _make_library(api, org, people, "curator-only-brand", kind="brand")
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "curator-project"}
    ).json()["project"]["id"]
    owner.put(
        f"/libraries/{project_id}/grants",
        json={"principal_id": str(people.colleague), "role": "curator"},
    )
    rule = world.rule(brand, title="curator-only-rule")

    refused = api.as_(people.colleague).post(
        f"/projects/{project_id}/rules", json={"rule_id": str(rule), "version_no": 1}
    )
    assert refused.status_code == 403, refused.text
    assert (
        world.count("SELECT count(*) FROM kb.project_rule_pin WHERE project_id = %s", (project_id,))
        == 0
    )

    # give the colleague curator on the brand library and it becomes possible
    assert (
        owner.put(
            f"/libraries/{brand}/grants",
            json={"principal_id": str(people.colleague), "role": "curator"},
        ).status_code
        == 200
    )
    assert (
        api.as_(people.colleague)
        .post(f"/projects/{project_id}/rules", json={"rule_id": str(rule), "version_no": 1})
        .status_code
        == 204
    )


def test_a_project_row_must_hang_off_a_project_library(run_sql, world):
    """A trigger, because a CHECK constraint cannot contain a subquery.

    Without it, a brand library could carry a project manifest, and the whole
    'a project is a library of kind project' premise would be decorative.
    """
    reference = world.library(name="not-a-project", kind="reference")
    rc, out = run_sql(
        "INSERT INTO kb.project (id, description) VALUES (%s, 'nope')", params=(reference,)
    )
    assert rc == 1, out
    assert "requires kind" in out, out
    assert world.count("SELECT count(*) FROM kb.project WHERE id = %s", (reference,)) == 0


def test_a_project_cannot_link_itself(run_sql, world):
    project = world.library(name="self-project", kind="project")
    world.project(project)
    # No principal on this session: the visibility guard steps aside for a
    # migration-role session, so the CHECK constraint is what refuses.
    rc, out = run_sql(
        "INSERT INTO kb.project_library_link (project_id, library_id) VALUES (%s, %s)",
        params=(project, project),
    )
    assert rc == 1, out
    assert "project_cannot_link_itself" in out, out
    assert (
        world.count(
            "SELECT count(*) FROM kb.project_library_link WHERE project_id = %s", (project,)
        )
        == 0
    )


def test_a_curator_cannot_probe_which_library_ids_exist(api, world, org, people):
    """The existence oracle, closed.

    A project curator may link libraries they can see. For every other id they
    must get the same answer the database gives for an id that was never real —
    otherwise "no such library" versus a silent success is a directory of the
    whole installation.
    """
    owner = api.as_(people.owner)
    closed = _make_library(api, org, people, "probe-target", kind="brand")
    absent = "00000000-0000-4000-8000-999999999999"
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "probe-project"}
    ).json()["project"]["id"]
    owner.put(
        f"/libraries/{project_id}/grants",
        json={"principal_id": str(people.colleague), "role": "curator"},
    )

    colleague = api.as_(people.colleague)
    real = colleague.post(f"/projects/{project_id}/libraries", json={"library_id": closed})
    imaginary = colleague.post(f"/projects/{project_id}/libraries", json={"library_id": absent})

    assert real.status_code >= 400, real.text
    assert imaginary.status_code == real.status_code, (real.text, imaginary.text)
    assert imaginary.json()["detail"] == real.json()["detail"], (real.text, imaginary.text)
    assert (
        world.count(
            "SELECT count(*) FROM kb.project_library_link WHERE project_id = %s", (project_id,)
        )
        == 0
    )

    # the owner, who can see it, links it — so the refusals above were the
    # visibility guard and not a broken route
    assert (
        owner.post(f"/projects/{project_id}/libraries", json={"library_id": closed}).status_code
        == 204
    )


def test_an_unlinked_or_empty_context_is_complete_not_an_error(api, world, org, people):
    """Nothing required means nothing blocked. A wrong 'blocked' is also a lie."""
    owner = api.as_(people.owner)
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "empty-project"}
    ).json()["project"]["id"]
    response = owner.get(f"/projects/{project_id}/context")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["links"] == [] and payload["pins"] == []
    assert payload["complete"] is True and payload["context_state"] == "complete"
    assert payload["project"]["kind"] == "project"


def test_unlinking_restores_a_complete_context(api, world, org, people):
    owner = api.as_(people.owner)
    brand = _make_library(api, org, people, "unlink-brand", kind="brand")
    project_id = owner.post(
        "/projects", json={"organisation_id": org, "name": "unlink-project"}
    ).json()["project"]["id"]
    owner.put(
        f"/libraries/{project_id}/grants",
        json={"principal_id": str(people.colleague), "role": "reader"},
    )
    owner.post(f"/projects/{project_id}/libraries", json={"library_id": brand})
    assert (
        api.as_(people.colleague).get(f"/projects/{project_id}/context").json()["complete"] is False
    )

    assert owner.delete(f"/projects/{project_id}/libraries/{brand}").status_code == 204
    assert (
        api.as_(people.colleague).get(f"/projects/{project_id}/context").json()["complete"] is True
    )


def db_of(api):
    return api._pool.connection()
