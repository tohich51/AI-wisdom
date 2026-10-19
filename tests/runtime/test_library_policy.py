"""Real SQL policies compose ownership, dependencies, client trust and export."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from psycopg.conninfo import make_conninfo
from test_research_grounding import parsed
from test_runtime import rule
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, transaction_identity
from kb.catalog import content, libraries, policies, writes
from kb.mcp.server import build_mcp
from kb.retrieval import semantic_registry

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]


def configure(conn, world, **flags):
    values = dict(external_processing_allowed=True, cloud_mcp_allowed=True, export_allowed=True)
    values.update(flags)
    return policies.put_policy(
        conn, world["principal"], world["lib"], policies.LibraryPolicyInput(**values)
    )


def grant(world, role):
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,%s)",
            (world["lib"], world["other"].principal_id, role),
        )


def test_local_only_policy_closes_cloud_data_and_external_index_but_keeps_local_read(world):
    original = rule(world)
    cloud = replace(world["principal"], cloud_client=True)
    with psycopg.connect(world["dsn"]) as conn:
        # No implicit permission for a new library to leave via a cloud client.
        assert libraries.list_libraries(conn, cloud).libraries == []
        with pytest.raises(AccessDenied):
            content.get_rule(conn, cloud, original["id"])
        configure(conn, world, external_processing_allowed=False, export_allowed=False)
        assert libraries.list_libraries(conn, cloud).libraries == []
        assert content.get_rule(conn, world["principal"], original["id"])["id"] == original["id"]
        with pytest.raises(AccessDenied):
            semantic_registry.capture(conn, world["principal"], world["lib"])
        with psycopg.connect(
            make_conninfo(world["admin"], user="kb_provisioner"), autocommit=True
        ) as provisioner:
            assert semantic_registry.pending_libraries(provisioner, rotation=0) == []
        with pytest.raises(AccessDenied):
            content.export_skill(conn, world["principal"], world["lib"], [original["id"]])
        configure(conn, world)
        assert content.get_rule(conn, cloud, original["id"])["id"] == original["id"]
        artifact = content.export_skill(conn, cloud, world["lib"], [original["id"]])
        assert artifact["manifest"]["content_sha256"] == artifact["sha256"]
        assert artifact["manifest"]["rules"][0]["version_no"] == 1


def test_draft_belongs_to_author_and_withdrawal_closes_full_rule_dependency(world):
    grant(world, "contributor")
    source_id = parsed(world, "A source supports the rule.")
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            fragment = conn.execute(
                "SELECT id FROM kb.fragment WHERE source_id=%s", (source_id,)
            ).fetchone()[0]
        draft = writes.create_rule(
            conn,
            world["principal"],
            world["lib"],
            writes.RuleInput(
                title="Own draft",
                when_to_apply="When measuring",
                actions=["Measure"],
                expected_effect="Evidence",
                fragment_ids=[fragment],
            ),
        )
        with pytest.raises(AccessDenied):
            content.get_rule(conn, world["other"], draft["id"])
        with transaction_identity(conn, world["other"]):
            assert (
                conn.execute(
                    "UPDATE kb.rule SET title='Forged' WHERE id=%s RETURNING id", (draft["id"],)
                ).fetchone()
                is None
            )
        writes.publish(conn, world["principal"], draft["id"])
        assert content.get_rule(conn, world["other"], draft["id"])["title"] == "Own draft"
        use = writes.start_use(conn, world["other"], draft["id"], 1)
        with transaction_identity(conn, world["principal"]):
            conn.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (source_id,))
        for principal in [world["principal"], world["other"]]:
            with pytest.raises(AccessDenied):
                content.get_rule(conn, principal, draft["id"])
            with pytest.raises(AccessDenied):
                content.export_skill(conn, principal, world["lib"], [draft["id"]])
        with transaction_identity(conn, world["other"]):
            assert (
                conn.execute("SELECT id FROM kb.use_record WHERE id=%s", (use["id"],)).fetchone()
                is None
            )


async def test_mcp_requires_action_scope_and_current_grant_independently(world, monkeypatch):
    monkeypatch.setenv("KB_MCP_RESOURCE_URL", "https://gateway.example/mcp")
    monkeypatch.setenv("KB_OIDC_ISSUER", "https://id.example/realms/kb")
    monkeypatch.setenv("KB_MCP_CLIENT_ID", "fixture")
    original = rule(world)
    with psycopg.connect(world["dsn"]) as conn:
        configure(conn, world)
    identity = SimpleNamespace(
        principal_id=world["principal"].principal_id,
        account_id=world["principal"].account_id,
        scopes=frozenset(),
        client_id="fixture",
    )

    bearer = str(uuid4())

    def resolve(token):
        assert token == bearer
        return identity

    app = SimpleNamespace(
        state=SimpleNamespace(
            resolve_token=resolve,
            db_connection=lambda: psycopg.connect(world["dsn"]),
        )
    )
    server, _ = build_mcp(app, ["gateway.example"])
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"authorization": "Bearer " + bearer})
        )
    )
    args = {"rule_id": str(original["id"])}
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("get_rule", args, context=ctx)
    identity.scopes = frozenset({"kb:read"})
    assert not (await server.call_tool("get_rule", args, context=ctx)).is_error
    # The same verified action scope cannot grant another member this library.
    identity.principal_id = world["other"].principal_id
    for _ in range(2):
        with pytest.raises(UnexpectedToolError):
            await server.call_tool("get_rule", args, context=ctx)
    identity.principal_id = world["principal"].principal_id
    with pytest.raises(UnexpectedToolError):
        await server.call_tool(
            "export_skill",
            {"library_id": str(world["lib"]), "rule_ids": [str(original["id"])]},
            context=ctx,
        )
