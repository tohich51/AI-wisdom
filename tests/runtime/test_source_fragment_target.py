"""Focused real-PostgreSQL coverage for direct source-fragment targeting."""

from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from test_runtime import runtime_db as runtime_db
from test_runtime import source as make_source
from test_runtime import world as world

from kb.access.policy import AccessDenied
from kb.catalog import content
from kb.contracts.mcp_tools import ReadSourceArgs
from kb.http import content as http_content
from kb.mcp.server import build_mcp

pytestmark = pytest.mark.integration


def _insert_fragments(world, source_id, ordinals):
    inserted = []
    with psycopg.connect(world["admin"]) as conn:
        for ordinal in ordinals:
            row = conn.execute(
                "INSERT INTO kb.fragment(source_id,ordinal,text,locator_kind) "
                "VALUES(%s,%s,%s,'none') RETURNING id,ordinal",
                (source_id, ordinal, f"Synthetic fragment at ordinal {ordinal}"),
            ).fetchone()
            inserted.append((row[0], row[1]))
    return inserted


def _source_event_count(world, source_id):
    with psycopg.connect(world["admin"]) as conn:
        return conn.execute(
            "SELECT count(*) FROM kb.retrieval_event WHERE item_kind=%s AND item_id=%s",
            ("source", source_id),
        ).fetchone()[0]


def test_fragment_id_starts_at_sparse_target_and_records_one_event(world):
    source_id = make_source(world, "Synthetic source for sparse fragment lookup.")
    fragments = _insert_fragments(world, source_id, (0, 650, 1200))
    target_id = fragments[1][0]
    before = _source_event_count(world, source_id)

    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(ValueError, match="from_fragment"):
            content.read_source(
                conn,
                world["principal"],
                source_id,
                -1,
                2,
                fragment_id=target_id,
            )
        with pytest.raises(ValueError, match="limit"):
            content.read_source(
                conn,
                world["principal"],
                source_id,
                0,
                0,
                fragment_id=target_id,
            )
        result = content.read_source(
            conn,
            world["principal"],
            source_id,
            from_fragment=0,
            limit=2,
            fragment_id=target_id,
        )

    assert set(result) == {
        "source",
        "fragments",
        "retrieved_only",
        "retrieval_event_id",
    }
    assert result["source"]["id"] == source_id
    assert [row["ordinal"] for row in result["fragments"]] == [650, 1200]
    assert all(row["source_id"] == source_id for row in result["fragments"])
    assert result["retrieved_only"] is True
    assert result["retrieval_event_id"] is not None
    assert _source_event_count(world, source_id) == before + 1


def test_default_source_read_is_unchanged(world):
    source_id = make_source(world, "Synthetic source for default fragment read.")
    fragments = _insert_fragments(world, source_id, (0, 650, 1200))

    with psycopg.connect(world["dsn"]) as conn:
        result = content.read_source(conn, world["principal"], source_id)

    assert set(result) == {
        "source",
        "fragments",
        "retrieved_only",
        "retrieval_event_id",
    }
    assert result["source"]["id"] == source_id
    assert [row["id"] for row in result["fragments"]] == [
        fragment_id for fragment_id, _ordinal in fragments
    ]
    assert [row["ordinal"] for row in result["fragments"]] == [0, 650, 1200]
    assert result["retrieved_only"] is True
    assert result["retrieval_event_id"] is not None


def test_cross_source_inaccessible_and_unknown_targets_are_uniformly_refused(world):
    visible_source = make_source(world, "Synthetic source owning the visible target.")
    other_source = make_source(world, "Synthetic source owning the foreign target.")
    visible = _insert_fragments(world, visible_source, (0, 650))
    foreign = _insert_fragments(world, other_source, (0,))
    before = _source_event_count(world, visible_source)
    cases = (
        (
            "cross-source",
            world["principal"],
            visible_source,
            foreign[0][0],
        ),
        (
            "unknown",
            world["principal"],
            visible_source,
            uuid4(),
        ),
        (
            "inaccessible-to-principal",
            world["other"],
            visible_source,
            visible[1][0],
        ),
    )

    for label, principal, source_id, fragment_id in cases:
        with psycopg.connect(world["dsn"]) as conn:
            with pytest.raises(AccessDenied) as raised:
                content.read_source(
                    conn,
                    principal,
                    source_id,
                    fragment_id=fragment_id,
                )
        assert str(raised.value) == "forbidden", label
        assert _source_event_count(world, visible_source) == before, label


async def test_real_adapters_forward_target_and_enforce_scope(monkeypatch):
    source_id, fragment_id, principal_id, account_id = (uuid4() for _ in range(4))
    admitted = ReadSourceArgs.model_validate(
        {"source_id": str(source_id), "fragment_id": str(fragment_id)}
    )
    assert admitted.fragment_id == fragment_id
    assert ReadSourceArgs(source_id=source_id).fragment_id is None
    for name in ("principal_id", "account_id", "role"):
        with pytest.raises(ValueError):
            ReadSourceArgs.model_validate({"source_id": str(source_id), name: str(uuid4())})
    calls, conn = [], object()
    identity = SimpleNamespace(
        principal_id=principal_id,
        account_id=account_id,
        scopes=frozenset({"kb:read"}),
    )

    def read(db, principal, supplied_source, offset, limit, *, fragment_id):
        assert db is conn
        assert principal.principal_id == principal_id
        assert principal.account_id == account_id
        calls.append((supplied_source, offset, limit, fragment_id))
        return {"source": {"id": str(supplied_source)}, "fragments": []}

    read.__name__ = "read_source"
    monkeypatch.setattr(content, "read_source", read)
    monkeypatch.setenv("KB_MCP_RESOURCE_URL", "https://mcp.example.test/mcp")
    monkeypatch.setenv("KB_OIDC_ISSUER", "https://issuer.example.test")
    monkeypatch.setenv("KB_MCP_CLIENT_ID", "fixture")
    app = SimpleNamespace(
        state=SimpleNamespace(
            resolve_token=lambda _token: identity, db_connection=lambda: nullcontext(conn)
        )
    )
    server, _ = build_mcp(app, ["mcp.example.test"])
    context = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"authorization": "Bearer fixture"})
        )
    )
    args = {
        "source_id": str(source_id),
        "from_fragment": 649,
        "limit": 2,
        "fragment_id": str(fragment_id),
    }
    assert not (await server.call_tool("read_source", args, context=context)).is_error
    assert calls == [(source_id, 649, 2, fragment_id)]
    identity.scopes = frozenset()
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("read_source", args, context=context)
    assert len(calls) == 1

    from kb.access.policy import Principal

    http_app = FastAPI()
    http_app.state.db_connection = lambda: nullcontext(conn)

    @http_app.middleware("http")
    async def fixture_identity(request, call_next):
        request.state.principal = Principal(principal_id, account_id)
        return await call_next(request)

    http_app.include_router(http_content.router, prefix="/api")
    with TestClient(http_app) as client:
        response = client.get(
            f"/api/sources/{source_id}",
            params={"from_fragment": 649, "limit": 2, "fragment_id": str(fragment_id)},
        )
        assert response.status_code == 200
        assert calls[-1] == (source_id, 649, 2, fragment_id)
        assert (
            client.get(f"/api/sources/{source_id}", params={"fragment_id": "invalid"}).status_code
            == 422
        )
    assert len(calls) == 2
