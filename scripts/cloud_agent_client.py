#!/usr/bin/env python3
"""Server-side MCP client for the dedicated MiniMax Cloud service identity."""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from uuid import UUID

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

APP = "https://ai-wisdom.150-241-115-134.sslip.io"
ISSUER = "https://id.ai-wisdom.150-241-115-134.sslip.io/realms/knowledge-hub"
TOOLS = frozenset(
    {
        "list_libraries",
        "search_knowledge",
        "read_source",
        "get_rule",
        "get_project_context",
        "start_use",
        "record_outcome",
        "submit_source",
        "job_status",
        "export_skill",
    }
)
MAX_JSON = 65536
UUID_PATH = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GET_PATH = re.compile(
    r"/api/(?:me|library-types|libraries|libraries/"
    + UUID_PATH
    + r"(?:/(?:sources|knowledge|rules|grants))?|projects/"
    + UUID_PATH
    + r"(?:/context)?)"
)
POST_PATH = re.compile(
    r"/api/(?:library-types|libraries|projects|projects/" + UUID_PATH + r"/(?:libraries|rules))"
)
PUT_PATH = re.compile(r"/api/libraries/" + UUID_PATH + r"/grants")


class Refused(Exception):
    """Only fixed public error codes are allowed in this exception."""


def strict_object(raw: str) -> dict:
    if len(raw.encode()) > MAX_JSON:
        raise Refused("json_too_large")

    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise Refused("duplicate_json_key")
            output[key] = value
        return output

    def constant(_):
        raise Refused("invalid_json_constant")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(value, dict):
        raise Refused("json_object_required")
    return value


def load_config(path: Path = Path("/config/client.json")) -> dict:
    settings = strict_object(path.read_text())
    expected = {
        "gateway_url": APP,
        "mcp_url": APP + "/mcp",
        "issuer": ISSUER,
        "token_endpoint": ISSUER + "/protocol/openid-connect/token",
        "client_id": "kb-minimax-cloud",
    }
    if any(settings.get(key) != value for key, value in expected.items()):
        raise Refused("configuration_refused")
    for key in ("principal_id", "account_id"):
        if str(UUID(settings[key])) != settings[key]:
            raise Refused("identity_configuration_refused")
    if not isinstance(settings.get("client_secret"), str) or not settings["client_secret"]:
        raise Refused("configuration_refused")
    return settings


def parse_command(args: list[str]) -> tuple[str, dict]:
    if args in (["doctor"], ["tools"]):
        return args[0], {}
    if len(args) == 3 and args[0] == "call" and args[1] in TOOLS:
        return "call", {"name": args[1], "arguments": read_arguments(args[2])}
    if len(args) in (3, 4) and args[0] == "api":
        method, path = args[1:3]
        if method == "GET" and len(args) == 3 and GET_PATH.fullmatch(path):
            return "api", {"method": method, "path": path, "json": None}
        if method == "POST" and len(args) == 4 and POST_PATH.fullmatch(path):
            return "api", {"method": method, "path": path, "json": read_arguments(args[3])}
        if method == "PUT" and len(args) == 4 and PUT_PATH.fullmatch(path):
            return "api", {"method": method, "path": path, "json": read_arguments(args[3])}
    raise Refused("command_refused")


def read_arguments(raw: str) -> dict:
    if raw == "-":
        raw = sys.stdin.buffer.read(MAX_JSON + 1).decode()
    return strict_object(raw)


async def execute(command: str, params: dict, settings: dict) -> dict:
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as http:
        response = await http.post(
            settings["token_endpoint"],
            data={
                "grant_type": "client_credentials",
                "client_id": settings["client_id"],
                "client_secret": settings["client_secret"],
                "scope": "openid",
            },
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        if not isinstance(token, str) or not token:
            raise Refused("authentication_failed")
        http.headers["Authorization"] = "Bearer " + token
        response = await http.get(APP + "/api/me")
        response.raise_for_status()
        identity = response.json()
        if any(identity.get(key) != settings[key] for key in ("principal_id", "account_id")):
            raise Refused("identity_mismatch")
        output = {"status": "ok", "transport": "mcp_via_ssh_server_client", "command": command}
        if command == "api":
            response = await http.request(
                params["method"], APP + params["path"], json=params["json"]
            )
            if not response.is_success:
                raise Refused("api_request_refused")
            return {
                **output,
                "transport": "https_api_via_ssh_server_client",
                "http_status": response.status_code,
                "result": response.json() if response.content else None,
            }
        async with streamable_http_client(settings["mcp_url"], http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if command == "call":
                    result = await session.call_tool(params["name"], params["arguments"])
                    if result.is_error:
                        raise Refused("mcp_tool_refused")
                    return {
                        **output,
                        "tool": params["name"],
                        "result": result.model_dump(mode="json", by_alias=True),
                    }
                tools = await session.list_tools()
                if command == "tools":
                    return {
                        **output,
                        "tools": [
                            tool.model_dump(mode="json", by_alias=True) for tool in tools.tools
                        ],
                    }
                return {
                    **output,
                    "identity": identity,
                    "tools": [tool.name for tool in tools.tools],
                }


def main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        command, params = parse_command(sys.argv[1:])
        result = asyncio.run(execute(command, params, load_config()))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, Refused) else "request_failed"
        print(json.dumps({"status": "error", "code": code}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
