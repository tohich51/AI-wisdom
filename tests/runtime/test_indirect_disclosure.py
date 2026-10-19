"""A20: catalogue picker and caller audit disclose only authorised data.

The current UI has no server autocomplete endpoint; the library picker uses the
catalogue and map filtering uses its already-authorised graph. This case covers
the actual HTTP catalogue/audit data, including meaningful own-data positives.
Existing upload dedup, uniform unknown/foreign evidence refusal and graph RLS
cases remain the other A20 legs; they are not duplicated here.
Real PostgreSQL and API routers; only identity resolution is a labelled fixture.
"""

import json

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_private_canary_matrix import _secrets
from test_private_canary_matrix import canary_world as canary_world
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.catalog import content, workspace
from kb.http.router import mount_api

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]


def _client(world, actor):
    """A real FastAPI app with the v1 and legacy routers mounted.

    ``db_connection`` hands each request its own connection on the shared
    instance, and the middleware installs the verified principal that the
    production auth layer would have resolved from the bearer token.
    """
    app = FastAPI()
    app.state.db_connection = lambda: psycopg.connect(world["dsn"])
    app.state.semantic_search = None

    @app.middleware("http")
    async def fixture_identity(request, call_next):
        request.state.principal = actor
        return await call_next(request)

    mount_api(app)
    return TestClient(app)


def test_catalogue_picker_and_caller_audit_hide_foreign_names_counts_and_events(
    world, canary_world
):
    alice, bob = canary_world["alice"], canary_world["bob"]
    for party in (bob, alice):
        with psycopg.connect(world["dsn"]) as conn:
            content.get_rule(conn, party["actor"], party["rule_id"])
            assert content.read_source(conn, party["actor"], party["source_id"])["fragments"]
            assert content.search_knowledge(conn, party["actor"], "privacyneedle")["hits"]
    with psycopg.connect(world["dsn"]) as conn:
        own = workspace.list_retrievals(conn, alice["actor"])
        foreign = workspace.list_retrievals(conn, bob["actor"])
    own_ids, foreign_ids = {str(r["id"]) for r in own}, {str(r["id"]) for r in foreign}
    assert own_ids and foreign_ids and not own_ids.intersection(foreign_ids)
    assert str(alice["source_id"]) in {str(r["item_id"]) for r in own}
    assert str(bob["source_id"]) in {str(r["item_id"]) for r in foreign}
    forbidden = _secrets(bob) + list(foreign_ids)
    with _client(world, alice["actor"]) as client:
        for _ in range(2):
            for prefix in ("/api/v1", "/api"):
                for params in ({}, {"kind": "reference"}, {"limit": 1, "offset": 1}):
                    response = client.get(prefix + "/libraries", params=params)
                    assert response.status_code == 200
                    page = response.json()
                    assert page["total"] == 1
                    assert [row["id"] for row in page["libraries"]] == (
                        [] if params.get("offset") else [str(alice["lib"])]
                    )
                    blob = json.dumps(page, ensure_ascii=False)
                    assert all(value not in blob for value in forbidden)
                response = client.get(prefix + "/retrievals")
                assert response.status_code == 200
                rows = response.json()
                assert {row["id"] for row in rows} == own_ids
                assert alice["title"] in {row["title"] for row in rows}
                blob = json.dumps(rows, ensure_ascii=False)
                assert all(value not in blob for value in forbidden)
                response = client.get(prefix + "/experience-summary")
                assert response.status_code == 200
                summary = response.json()
                assert summary["visible_issuance_count"] == len(own_ids)
                assert summary["visible_issuance_count"] < len(own_ids) + len(foreign_ids)
                blob = json.dumps(summary, ensure_ascii=False)
                assert all(value not in blob for value in forbidden)
