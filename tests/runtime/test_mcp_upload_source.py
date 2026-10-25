"""MCP local originals: real SQL/storage, replay, parsing and denied writes."""

import base64
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.catalog import content, upload, writes
from kb.contracts.mcp_tools import MCP_MAX_FILE_BYTES, UploadSourceArgs
from kb.jobs.ingestion import run_job
from kb.mcp import uploads
from kb.mcp.server import build_mcp

DEFAULT_TEXT = "Начало книги.\nПравило: проверить результат.\n".encode()  # noqa: RUF001


def arguments(world, raw=DEFAULT_TEXT):
    return {
        "library_id": str(world["lib"]),
        "title": "Сборник · принципы",
        "filename": "principles.md",
        "idempotency_key": str(uuid4()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_text": raw.decode(),
    }


def adapter(world, monkeypatch):
    monkeypatch.setenv("KB_MCP_RESOURCE_URL", "https://gateway.example/mcp")
    monkeypatch.setenv("KB_OIDC_ISSUER", "https://id.example/realms/kb")
    monkeypatch.setenv("KB_MCP_CLIENT_ID", "fixture")
    monkeypatch.setenv("KB_MCP_LOCAL_CLIENT_IDS", "fixture")
    identity = SimpleNamespace(
        principal_id=world["principal"].principal_id,
        account_id=world["principal"].account_id,
        scopes=frozenset({"kb:read", "kb:contribute"}),
        client_id="fixture",
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            resolve_token=lambda token: identity,
            db_connection=lambda: psycopg.connect(world["dsn"]),
            blob_store=world["store"],
        )
    )
    server, _ = build_mcp(app, ["gateway.example"])
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers={"authorization": "Bearer synthetic-fixture"})
        )
    )
    return server, ctx, identity


async def invoke(server, ctx, args):
    result = await server.call_tool("upload_source", args, context=ctx)
    assert not result.is_error
    wire = result.model_dump(mode="json", by_alias=True)
    return wire.get("structuredContent") or json.loads(wire["content"][0]["text"])


def counts(world):
    with psycopg.connect(world["admin"]) as conn:
        return conn.execute(
            "SELECT (SELECT count(*) FROM kb.source WHERE library_id=%s), "
            "(SELECT count(*) FROM kb.job WHERE library_id=%s)",
            (world["lib"], world["lib"]),
        ).fetchone()


async def test_mcp_original_replay_parse_and_changed_payload_refusal(world, monkeypatch):
    server, ctx, _ = adapter(world, monkeypatch)
    args = arguments(world)
    first = await invoke(server, ctx, args)
    repeated = await invoke(server, ctx, args)
    assert first["source_id"] == repeated["source_id"]
    assert first["job_id"] == repeated["job_id"]
    assert repeated["status"] == "replayed" and repeated["duplicate_of_existing"]
    assert first["publication"] == "draft" and first["sha256"] == args["sha256"]
    assert counts(world) == (1, 1)
    changed = arguments(world, b"other content")
    changed["idempotency_key"] = args["idempotency_key"]
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("upload_source", changed, context=ctx)
    assert counts(world) == (1, 1)
    source_id, job_id = UUID(first["source_id"]), UUID(first["job_id"])
    with psycopg.connect(world["dsn"]) as conn:
        original = upload.read_original(conn, world["principal"], world["store"], source_id)
        assert original.content == args["content_text"].encode()
        assert not writes.job_status(conn, world["principal"], job_id)["extraction_enabled"]
    run_job(world["worker"], job_id, world["principal"], world["store"].root)
    with psycopg.connect(world["dsn"]) as conn:
        read = content.read_source(conn, world["principal"], source_id)
        assert read["source"]["processing"] == "done"
        assert read["source"]["publication"] == "draft"
        assert [f["text"] for f in read["fragments"]] == args["content_text"].splitlines()
        assert writes.job_status(conn, world["principal"], job_id)["status"] == "done"


async def test_mcp_binary_store_only_and_library_dedup(world, monkeypatch):
    server, ctx, _ = adapter(world, monkeypatch)
    raw = b"%PDF-1.7\nopaque original\x00\xff"
    args = arguments(world)
    args.pop("content_text")
    args.update(
        filename="original.pdf",
        sha256=hashlib.sha256(raw).hexdigest(),
        content_base64=base64.b64encode(raw).decode(),
        process=False,
    )
    first = await invoke(server, ctx, args)
    args["idempotency_key"] = str(uuid4())
    duplicate = await invoke(server, ctx, args)
    assert duplicate["source_id"] == first["source_id"] and duplicate["status"] == "deduplicated"
    assert first["job_id"] is None and first["media_type"] == "application/pdf"
    assert counts(world) == (1, 0)
    with psycopg.connect(world["dsn"]) as conn:
        original = upload.read_original(
            conn, world["principal"], world["store"], UUID(first["source_id"])
        )
        assert original.content == raw


async def test_mcp_scope_grant_cloud_policy_and_revoked_replay(world, monkeypatch):
    server, ctx, identity = adapter(world, monkeypatch)
    args = {**arguments(world), "process": False}
    identity.scopes = frozenset({"kb:read"})
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("upload_source", args, context=ctx)
    identity.scopes = frozenset({"kb:contribute"})
    identity.principal_id = world["other"].principal_id
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("upload_source", args, context=ctx)
    identity.principal_id = world["principal"].principal_id
    identity.client_id = "untrusted-cloud-client"
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("upload_source", args, context=ctx)
    assert counts(world) == (0, 0)
    assert not [p for p in world["store"].root.rglob("*") if p.is_file()]
    identity.client_id = "fixture"
    identity.principal_id = world["other"].principal_id
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) "
            "VALUES(%s,%s,'contributor')",
            (world["lib"], world["other"].principal_id),
        )
    await invoke(server, ctx, args)
    with psycopg.connect(world["admin"]) as conn:
        conn.execute(
            "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
            (world["lib"], world["other"].principal_id),
        )
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("upload_source", args, context=ctx)
    assert counts(world) == (1, 0)


async def test_retry_after_processing_admission_failure_keeps_one_original(world, monkeypatch):
    server, ctx, _ = adapter(world, monkeypatch)
    args = arguments(world)
    actual = uploads.submit_processing

    def unavailable(*args, **kwargs):
        raise ValueError("processing_queue_full")

    monkeypatch.setattr(uploads, "submit_processing", unavailable)
    with pytest.raises(UnexpectedToolError):
        await server.call_tool("upload_source", args, context=ctx)
    assert counts(world) == (1, 0)
    monkeypatch.setattr(uploads, "submit_processing", actual)
    result = await invoke(server, ctx, args)
    assert result["status"] == "replayed" and result["job_id"]
    assert counts(world) == (1, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"content_text": None},
        {"content_base64": "YQ=="},
        {"filename": "../book.md"},
        {"filename": "dir\\book.md"},
        {"filename": "bad\nname.md"},
        {"content_text": ""},
        {"media_type": "application/pdf"},
        {"process": False, "extract": True},
        {"principal_id": str(uuid4())},
    ],
)
def test_upload_argument_boundaries(changes):
    with pytest.raises(ValueError):
        UploadSourceArgs.model_validate({**arguments({"lib": uuid4()}), **changes})


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "0" * 64},
        {"content_text": "\ud800"},
        {"content_text": "я" * 600_000},
        {"content_text": None, "content_base64": "!!!!"},
        {"content_text": None, "content_base64": "YQ==\n"},
        {
            "content_text": None,
            "content_base64": base64.b64encode(b"a" * (MCP_MAX_FILE_BYTES + 1)).decode(),
        },
    ],
)
def test_invalid_bytes_are_refused_before_storage(changes):
    with pytest.raises(ValueError):
        uploads.decode_upload(
            UploadSourceArgs.model_validate({**arguments({"lib": uuid4()}), **changes})
        )
