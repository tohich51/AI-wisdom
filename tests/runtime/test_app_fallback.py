"""The /app/* SPA fallback, and the routes it must not swallow.

CONTRACTS.md: "SPA fallback only /app/*, including Reload; F prepares it for
C". F owns the route shape and the scoping. C owns the static side, so nothing
here reads or asserts an asset beyond the shell itself.

The scoping half is the part that can do damage, so it is tested as carefully
as the serving half: a fallback that is one path too wide would answer /api,
/mcp, /auth, /healthz and every mistyped URL with a 200 and an HTML body, and
the diff that introduces it looks identical to the one that fixes a 404.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kb.gateway import create_app

#: The screen addresses the frozen contract names, with the query strings it
#: names. A client-side router owns these paths, so a cold deep link and a
#: Reload both arrive as a GET that matches no API route.
CONTRACT_SCREENS = [
    "/app",
    "/app/libraries/11111111-1111-1111-1111-111111111111/sources",
    "/app/libraries/11111111-1111-1111-1111-111111111111/knowledge",
    "/app/libraries/11111111-1111-1111-1111-111111111111/rules",
    "/app/projects/11111111-1111-1111-1111-111111111111/context",
    "/app/research/22222222-2222-2222-2222-222222222222",
    "/app/maps?library=11111111-1111-1111-1111-111111111111&view=semantic",
    "/app/experience",
    "/app/settings/access",
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    from test_operation_registry import REAL_APP_ENV

    for key, value in REAL_APP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KB_BLOB_ROOT", str(tmp_path))
    # Not entered as a context manager on purpose: the lifespan opens a real
    # connection pool, and none of these assertions need it. The routes under
    # test are wired at create_app time.
    # The app trusts KB_ALLOWED_HOSTS, so the client has to present that host.
    return TestClient(create_app(), base_url="http://gateway.example")


@pytest.mark.parametrize("screen", CONTRACT_SCREENS)
def test_every_contract_screen_serves_the_shell(client, screen):
    response = client.get(screen)
    assert response.status_code == 200, screen
    assert response.headers["content-type"].startswith("text/html")


def test_a_reload_picks_up_a_new_shell(client):
    """ "Including Reload": the shell must not be cached, or a Reload keeps
    serving the previous deployment's document."""
    for _attempt in range(2):
        response = client.get("/app/experience")
        assert response.headers["cache-control"] == "no-cache"


def test_a_deep_link_below_the_contract_addresses_still_serves_the_shell(client):
    """The router owns the shape of /app, not the server. An address the
    contract does not enumerate today must not be a 404 today either."""
    assert client.get("/app/a/deep/path/that/no/card/mentions").status_code == 200
    assert client.get("/app/").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/healthz",
        "/readyz",
        "/auth/session",
        "/static/style.css",
        "/api/v1/libraries",
        "/api/libraries",
        "/mcp",
        "/not-a-route-at-all",
        "/application",  # a near miss outside the /app prefix boundary
        "/appx/something",
    ],
)
def test_the_fallback_is_not_a_root_wide_catch_all(client, path):
    """Everything the fallback must leave alone.

    The comparison is against the shell the fallback actually serves, so this
    holds for whatever markup C ships: a root-wide catch-all would answer every
    one of these with that exact body. The last two paths matter most, because
    a prefix boundary that also matched ``/appx`` or ``/application`` is a
    widening nothing else would catch.
    """
    shell = client.get("/app").text
    assert "html" in client.get("/app").headers["content-type"]
    assert client.get(path).text != shell, path


def test_the_shell_is_not_advertised_as_api(client):
    """The fallback is a browser concern and stays out of the schema, so the
    OpenAPI document still describes exactly the two API prefixes."""
    assert not [p for p in client.app.openapi()["paths"] if p.startswith("/app")]
