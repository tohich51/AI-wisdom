"""Client boundary: untrusted args cannot become credentials or arbitrary HTTP."""

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "cloud_client", Path(__file__).resolve().parents[2] / "scripts/cloud_agent_client.py"
)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


@pytest.mark.parametrize(
    "raw",
    [
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":Infinity}',
        "[1]",
        "null",
        '"text"',
        '{"x":"' + ("x" * 65536) + '"}',
    ],
)
def test_invalid_json_refused(raw):
    with pytest.raises((client.Refused, ValueError)):
        client.strict_object(raw)


@pytest.mark.parametrize(
    "args",
    [
        ["api", "GET", "https://attacker.test"],
        ["api", "GET", "/api/../auth"],
        ["api", "GET", "/api/me?x=1"],
        ["api", "GET", "/api/me#fragment"],
        ["api", "GET", "/api/%6de"],
        ["api", "GET", "/api/me/"],
        ["api", "POST", "/api/orgs/any/invitations", "{}"],
        ["api", "DELETE", "/api/libraries", "{}"],
        ["api", "POST", "/auth/login", "{}"],
        ["call", "read_token", "{}"],
        ["call", "list_libraries", "[]"],
        ["api", "GET", "/api/me", "{}"],
        ["doctor", "extra"],
    ],
)
def test_commands_cannot_escape_product_allowlist(args):
    with pytest.raises((client.Refused, ValueError)):
        client.parse_command(args)


def test_public_commands_and_allowed_config_paths():
    assert client.parse_command(["doctor"])[0] == "doctor"
    assert client.parse_command(["tools"])[0] == "tools"
    assert client.parse_command(["call", "list_libraries", "{}"]) == (
        "call",
        {"name": "list_libraries", "arguments": {}},
    )
    assert client.parse_command(["api", "POST", "/api/libraries", "{}"]) == (
        "api",
        {"method": "POST", "path": "/api/libraries", "json": {}},
    )
    path = "/api/libraries/00000000-0000-4000-8000-000000000001/grants"
    assert client.parse_command(["api", "PUT", path, "{}"]) == (
        "api",
        {"method": "PUT", "path": path, "json": {}},
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("token_endpoint", "https://attacker.test/token"),
        ("mcp_url", "http://localhost/mcp"),
        ("client_id", "kb-mcp"),
        ("principal_id", "owner"),
        ("client_secret", ""),
    ],
)
def test_config_cannot_reuse_owner_or_redirect_credentials(tmp_path, key, value):
    settings = {
        "gateway_url": client.APP,
        "mcp_url": client.APP + "/mcp",
        "issuer": client.ISSUER,
        "token_endpoint": client.ISSUER + "/protocol/openid-connect/token",
        "client_id": "kb-minimax-cloud",
        "client_secret": "test-only-not-a-credential",
        "principal_id": "00000000-0000-4000-8000-000000000001",
        "account_id": "00000000-0000-4000-8000-000000000002",
    }
    settings[key] = value
    path = tmp_path / "test-config.json"
    path.write_text(json.dumps(settings))
    with pytest.raises((client.Refused, ValueError)):
        client.load_config(path)
