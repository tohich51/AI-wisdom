import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kb.mcp.server import build_mcp

METADATA_ROOT = "/.well-known/oauth-protected-resource"
CANONICAL = f"{METADATA_ROOT}/mcp"
RESOURCE = "https://mcp.example.test/mcp"


class FakeApp:
    def __init__(self) -> None:
        self.authentication_calls: list[str] = []
        self.state = SimpleNamespace(resolve_token=self.resolve_token)

    def resolve_token(self, token: str) -> None:
        self.authentication_calls.append(token)
        raise AssertionError("public metadata must not resolve an authentication token")


async def _assert_public_discovery(child: Starlette, fake_app: FakeApp) -> None:
    transport = httpx.ASGITransport(app=child)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://mcp.example.test"
    ) as client:
        root_get = await client.get(METADATA_ROOT)
        canonical_get = await client.get(CANONICAL)

        assert root_get.status_code == canonical_get.status_code == 200
        assert root_get.content == canonical_get.content
        assert canonical_get.json()["resource"] == RESOURCE

        root_headers = root_get.headers
        canonical_headers = canonical_get.headers
        for header in (
            "content-type",
            "cache-control",
            "access-control-allow-origin",
            "access-control-expose-headers",
            "vary",
        ):
            assert root_headers.get(header) == canonical_headers.get(header)
        assert "public" in canonical_headers["cache-control"].lower()

        preflight_headers = {
            "Origin": "https://client.example",
            "Access-Control-Request-Method": "GET",
        }
        root_options = await client.options(METADATA_ROOT, headers=preflight_headers)
        canonical_options = await client.options(CANONICAL, headers=preflight_headers)

        assert root_options.status_code == canonical_options.status_code
        assert root_options.status_code in {200, 204}
        assert root_options.content == canonical_options.content

        root_headers = root_options.headers
        canonical_headers = canonical_options.headers
        for header in (
            "cache-control",
            "access-control-allow-origin",
            "access-control-allow-methods",
            "access-control-allow-headers",
            "access-control-max-age",
            "vary",
        ):
            assert root_headers.get(header) == canonical_headers.get(header)
        assert canonical_headers.get("access-control-allow-origin") is not None
        allowed_methods = canonical_headers["access-control-allow-methods"].lower()
        assert "get" in allowed_methods

        unknown = await client.get(f"{METADATA_ROOT}/unknown")
        assert unknown.status_code == 404
        assert fake_app.authentication_calls == []


def test_public_protected_resource_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_RESOURCE_URL", RESOURCE)
    monkeypatch.setenv("KB_OIDC_ISSUER", "https://issuer.example.test")
    fake_app = FakeApp()
    _, child = build_mcp(fake_app, hosts=["mcp.example.test"])

    root_routes = [route for route in child.routes if route.path == METADATA_ROOT]
    canonical_routes = [route for route in child.routes if route.path == CANONICAL]

    assert len(root_routes) == 1
    assert canonical_routes
    root_route = root_routes[0]
    canonical_route = canonical_routes[0]
    assert root_route.endpoint is canonical_route.endpoint
    assert root_route.methods == canonical_route.methods

    asyncio.run(_assert_public_discovery(child, fake_app))

    with TestClient(child, base_url="https://mcp.example.test") as client:
        unauthorized = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-test", "version": "1"},
                },
            },
        )

    assert unauthorized.status_code == 401
    expected_resource_metadata = (
        'resource_metadata="https://mcp.example.test/.well-known/oauth-protected-resource/mcp"'
    )
    assert expected_resource_metadata in unauthorized.headers["www-authenticate"]
