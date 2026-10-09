"""C09 — the library type registry is extensible, and the extension is inert.

"Extensible" here means a *data* extension: registering a type is an INSERT, it
needs no DDL, and it moves no already-loaded source. The tests below prove that
on a real database rather than asserting it in a docstring, because the
alternative failure mode is a type system that looks extensible in the API and
turns out to be an enum underneath.

They also prove the limits honestly. ``kb.library.kind`` is a PostgreSQL ENUM
created in 0001, and enums are closed, so a newly registered type is known to
the registry and the API *before* it can back a library. Pretending otherwise
would be the more comfortable lie and the more expensive one.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration

CORE = {"reference", "brand", "project", "playbook", "experience"}


@pytest.fixture
def loaded(world, people):
    """A library with content, of a kind nobody is changing."""
    lib = world.library(name="loaded-reference", kind="reference", audience_scope="all_invited")
    world.grant(lib, people.owner, "manager")
    sources = [world.source(lib, f"loaded-source-{i}") for i in range(3)]
    return {"library": lib, "sources": sources}


def test_the_five_core_types_are_registered(api, people):
    response = api.as_(people.owner).get("/library-types")
    assert response.status_code == 200, response.text
    types = response.json()
    assert {t["key"] for t in types} == CORE
    for entry in types:
        assert entry["is_core"] is True
        assert entry["title"], entry
        assert entry["template_version"] >= 1
        assert entry["review_process"], entry
        # an unknown description stays null; it is never invented
        assert entry["description"] is None or isinstance(entry["description"], str)


def test_registration_needs_no_ddl_and_disturbs_nothing(api, world, people, loaded, run_sql):
    """The extensibility claim, made measurable.

    Register a sixth type and compare everything that already existed: the
    library, its three sources, and the total count. If extending the catalogue
    required a data migration, this is where it would show.
    """
    before_libraries = world.count("SELECT count(*) FROM kb.library")
    before_sources = world.count(
        "SELECT count(*) FROM kb.source WHERE library_id = %s", (loaded["library"],)
    )

    response = api.as_(people.owner).post(
        "/library-types",
        json={
            "key": "runbook",
            "title": "Runbook",
            "description": "Operational procedure, kept beside the playbooks.",
            "template_version": 1,
            "allowed_extra_fields": [{"name": "service", "type": "text"}],
            "review_process": {"steps": ["extract", "curator_publish"]},
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["key"] == "runbook"
    assert response.json()["is_core"] is False

    # no migration of anything already stored
    assert world.count("SELECT count(*) FROM kb.library") == before_libraries
    assert (
        world.count("SELECT count(*) FROM kb.source WHERE library_id = %s", (loaded["library"],))
        == before_sources
        == 3
    )
    assert (
        world.count("SELECT count(*) FROM kb.source WHERE library_id = %s", (loaded["library"],))
        == 3
    )

    # and the existing library still resolves to its registered type
    rc, out = run_sql(
        "SELECT t.title FROM kb.library l JOIN kb.library_type t ON t.key = l.kind::text "
        "WHERE l.id = %s",
        params=(loaded["library"],),
    )
    assert (rc, out.strip()) == (0, "Reference"), out

    listed = api.as_(people.owner).get("/library-types").json()
    assert {t["key"] for t in listed} == CORE | {"runbook"}
    # core first, so the catalogue is stable in the UI
    assert [t["key"] for t in listed][:5] == [
        "brand",
        "experience",
        "playbook",
        "project",
        "reference",
    ]


def test_registration_grants_nobody_anything(api, people, run_sql, world):
    """A type is vocabulary. It carries no audience and creates no grant.

    A registry entry that widened access would make 'what kind is this library'
    an access-control input, which is precisely the coupling PRODUCT-SPEC
    forbids.
    """
    before = world.count("SELECT count(*) FROM kb.library_grant")
    response = api.as_(people.owner).post(
        "/library-types", json={"key": "policy_note", "title": "Policy note"}
    )
    assert response.status_code == 201, response.text
    assert world.count("SELECT count(*) FROM kb.library_grant") == before

    rc, out = run_sql(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'kb' "
        "AND table_name = 'library_type' AND column_name LIKE '%audience%'"
    )
    assert (rc, out.strip()) == (0, "0"), "the registry must not have an audience axis"


def test_the_runtime_cannot_rewrite_or_retire_a_type(api, world, people, run_sql):
    """Append-only from the runtime's point of view.

    Registration is configuration and is open. Editing or retiring a type is a
    migration: a retired type in use would change what existing libraries mean.
    The privileges are absent AND there is no policy, and the row is checked
    afterwards rather than assuming the error was the whole story.
    """
    before = world.count(
        "SELECT count(*) FROM kb.library_type WHERE key = 'brand' AND title = 'Brand' "
        "AND retired_at IS NULL"
    )
    assert before == 1

    for statement in (
        "UPDATE kb.library_type SET title = 'Hijacked' WHERE key = 'brand'",
        "DELETE FROM kb.library_type WHERE key = 'brand'",
    ):
        rc, out = run_sql(statement, role="kb_app", principal=people.owner)
        assert rc == 1, f"{statement!r} was not refused: {out}"
        assert "permission denied" in out.lower(), out

    assert (
        world.count(
            "SELECT count(*) FROM kb.library_type WHERE key = 'brand' AND title = 'Brand' "
            "AND retired_at IS NULL"
        )
        == 1
    ), "the refused write changed the row"

    listed = {t["key"] for t in api.as_(people.owner).get("/library-types").json()}
    assert "brand" in listed


def test_a_duplicate_registration_is_a_conflict_not_an_overwrite(api, people):
    api.as_(people.owner).post("/library-types", json={"key": "glossary", "title": "Glossary"})
    again = api.as_(people.owner).post(
        "/library-types", json={"key": "glossary", "title": "Stolen"}
    )
    assert again.status_code == 409, again.text
    titles = {t["key"]: t["title"] for t in api.as_(people.owner).get("/library-types").json()}
    assert titles["glossary"] == "Glossary"


def test_a_retired_type_stops_backing_new_libraries_but_keeps_the_old_ones(
    world, people, api, run_sql
):
    """The honest limit of the enum, tested rather than described.

    Retiring a type is done by the migration role, not the API. After it, a new
    library of that kind is refused by the registration trigger, and a library
    that already exists keeps resolving to a type row — because the row is only
    marked retired, never deleted.
    """
    library = world.library(name="playbook-already-there", kind="playbook")
    world.grant(library, people.owner, "manager")

    world.conn.execute("UPDATE kb.library_type SET retired_at = now() WHERE key = 'playbook'")
    try:
        rc, out = run_sql(
            "INSERT INTO kb.library (id, organisation_id, name, kind) "
            "VALUES (%s, %s, 'should-not-exist', 'playbook')",
            params=(uuid.uuid4(), world.organisation()),
        )
        assert rc == 1, f"a library of a retired type was accepted: {out}"
        assert "not registered" in out, out
        assert world.count("SELECT count(*) FROM kb.library WHERE name = 'should-not-exist'") == 0

        # the existing one is untouched and still resolvable
        listed = {t["key"] for t in api.as_(people.owner).get("/library-types").json()}
        assert "playbook" not in listed
        with_retired = {
            t["key"]
            for t in api.as_(people.owner).get("/library-types?include_retired=true").json()
        }
        assert "playbook" in with_retired
        assert world.count("SELECT count(*) FROM kb.library WHERE id = %s", (library,)) == 1
        assert (
            world.count(
                "SELECT count(*) FROM kb.library_type t JOIN kb.library l "
                "ON l.kind::text = t.key WHERE l.id = %s",
                (library,),
            )
            == 1
        )
    finally:
        world.conn.execute("UPDATE kb.library_type SET retired_at = NULL WHERE key = 'playbook'")
