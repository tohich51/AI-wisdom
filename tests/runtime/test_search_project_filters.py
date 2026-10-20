"""Real PostgreSQL C17 filter/context boundaries; no product-model/provider calls."""

import json
from uuid import uuid4

import psycopg
import pytest
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world
from test_search_pagination import QUERY, _seed

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import content, libraries, projects

pytestmark = pytest.mark.integration


def test_project_scope_reports_required_missing_context_without_closed_identifiers(world):
    principal = world["principal"]
    with psycopg.connect(world["dsn"]) as conn:
        project = projects.create_project(
            conn,
            principal,
            projects.CreateProject(organisation_id=principal.account_id, name="Filter project"),
        ).project.id
        closed = libraries.create_library(
            conn,
            principal,
            libraries.CreateLibrary(
                organisation_id=principal.account_id, name="Hidden brand", kind="brand"
            ),
        )
        projects.link_library(
            conn, principal, project, projects.LinkLibrary(library_id=world["lib"])
        )
        projects.link_library(conn, principal, project, projects.LinkLibrary(library_id=closed.id))
        linked = _seed(conn, principal, world["lib"], count=2)
        own = _seed(conn, principal, project, count=1)
        _seed(conn, principal, closed.id, count=1)
        unrelated = libraries.create_library(
            conn,
            principal,
            libraries.CreateLibrary(
                organisation_id=principal.account_id, name="Unrelated", kind="reference"
            ),
        )
        _seed(conn, principal, unrelated.id, count=1)
        libraries.revoke_grant(conn, principal, closed.id, principal.principal_id)
        result = content.search_knowledge(conn, principal, QUERY, project_id=project, offset=0)
        assert {h["id"] for h in result["hits"]} == set(linked + own)
        context = result["project_context"]
        assert context["unresolved_dependencies"] == ["linked_library_access_missing"]
        assert context["manifest"]["complete"] is False
        encoded = json.dumps(context)
        assert closed.name not in encoded and str(closed.id) not in encoded
        typed = content.search_knowledge(
            conn, principal, QUERY, project_id=project, library_types=["reference"]
        )
        assert {h["id"] for h in typed["hits"]} == set(linked)
        intersected = content.search_knowledge(
            conn, principal, QUERY, [unrelated.id], project_id=project
        )
        assert intersected["hits"] == []
        assert intersected["project_context"]["manifest"]["complete"] is False
        with pytest.raises(AccessDenied):
            content.search_knowledge(conn, world["other"], QUERY, project_id=project)
        with pytest.raises(AccessDenied):
            content.search_knowledge(conn, principal, QUERY, project_id=uuid4())

    detached = content.search_knowledge(
        None,
        principal,
        QUERY,
        project_id=project,
        connections=lambda: psycopg.connect(world["dsn"]),
    )
    assert {hit["id"] for hit in detached["hits"]} == set(linked + own)
    assert detached["project_context"]["manifest"]["complete"] is False


def test_custom_types_empty_scope_detached_connection_and_late_scope_change(world):
    principal = world["principal"]
    key = "filter_" + uuid4().hex[:12]
    with psycopg.connect(world["dsn"]) as conn:
        libraries.register_library_type(
            conn, principal, libraries.RegisterLibraryType(key=key, title="Custom filter")
        )
        typed = libraries.create_library(
            conn,
            principal,
            libraries.CreateLibrary(
                organisation_id=principal.account_id, name="Typed", kind="reference", type_key=key
            ),
        )
        ids = _seed(conn, principal, typed.id, count=2)
        assert {
            h["id"]
            for h in content.search_knowledge(conn, principal, QUERY, library_types=[key])["hits"]
        } == set(ids)

    class MustNotCall:
        def search(self, *args, **kwargs):
            raise AssertionError("empty filters must not invoke the provider/planner")

    def factory():
        return psycopg.connect(world["dsn"])

    for filters in (
        {"library_types": []},
        {"library_types": ["not_registered"]},
        {"library_ids": []},
    ):
        result = content.search_knowledge(
            None,
            principal,
            QUERY,
            semantic=MustNotCall(),
            connections=factory,
            offset=10001,
            **filters,
        )
        assert result["hits"] == [] and result["retrieval_event_ids"] == []
        assert result["page"]["has_more"] is False
        assert result["page"]["next_offset"] is None
    for bad in (["reference"] * 51, ["reference\n"], "reference"):
        with pytest.raises(ValueError):
            content.search_knowledge(None, principal, QUERY, connections=factory, library_types=bad)

    def events():
        with psycopg.connect(world["dsn"]) as c, transaction_identity(c, principal):
            return c.execute("SELECT count(*) FROM kb.retrieval_event").fetchone()[0]

    class ChangeTypeDuringExternalPhase:
        def search(self, conn, caller, query, scopes, limit, **kwargs):
            assert conn is None
            with psycopg.connect(world["dsn"]) as c:
                result = content.lexical_search_knowledge(c, caller, query, scopes, limit)
            with psycopg.connect(world["admin"]) as c:
                c.execute("UPDATE kb.library SET type_key=NULL WHERE id=%s", (typed.id,))
            return result

    before = events()
    with pytest.raises(AccessDenied):
        content.search_knowledge(
            None,
            principal,
            QUERY,
            connections=factory,
            library_types=[key],
            semantic=ChangeTypeDuringExternalPhase(),
        )
    assert events() == before  # refused results/context were not recorded as delivered
