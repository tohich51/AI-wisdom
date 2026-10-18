"""Regression tests for the shared operation registry.

These are the checks that keep three copies of the truth from drifting again:
the router, the MCP contract and the bearer scope table. Everything asserted
here is about parity and about an operation being reachable under the prefix a
client actually calls. No test in this file needs a database, a browser or a
running gateway.
"""

from __future__ import annotations

import base64

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from kb.access.bearer_policy import HANDLER_SCOPES, http_scope, scope_for_request
from kb.contracts.mcp_tools import EXPECTED_TOOLS, assert_no_identity_injection
from kb.contracts.operations import (
    ALIAS_PREFIX,
    API_PREFIXES,
    CANONICAL_PREFIX,
    CONTRACT_REVISION,
    MCP_OPERATIONS,
    OPERATIONS,
    OPERATIONS_BY_NAME,
    PAGINATED_OPERATIONS,
    Effect,
    assert_registry_consistent,
    canonical_api_path,
    operation_for,
    unclaimed_routes,
)
from kb.gateway import create_app
from kb.http.router import (
    ROUTERS,
    assert_prefix_parity,
    assert_routes_match_registry,
    build_api_router,
    inventory,
    mount_api,
    served_routes,
)

# The MCP contract calls a tool by its tool name; the bearer policy keys the
# scope by the handler function that serves it. Those two names are not always
# the same, and that mapping is exactly what a reviewer must be able to read
# rather than infer. Adding a tool means adding a row here.
TOOL_HANDLER: dict[str, str] = {
    "approve_browser_login": "approve_browser_login",
    "list_libraries": "list_libraries",
    "search_knowledge": "search_knowledge",
    "read_source": "read_source",
    "get_rule": "get_rule",
    "publish_knowledge": "publish",
    "get_project_context": "project_context",
    "start_use": "start_use",
    "record_outcome": "record_outcome",
    "submit_source": "submit_url",
    "job_status": "job_status",
    "export_skill": "export_skill",
    "list_jobs": "list_jobs",
    "list_retrievals": "list_retrievals",
    "publish_experience": "publish_experience",
    "list_shared_experience": "list_shared",
    "read_library_policy": "get_policy",
    "get_experience_summary": "experience_summary",
    "review_items": "review_batch",
    "edit_knowledge": "edit_knowledge",
    "research_sources": "list_research_sources",
    "list_research": "list_research",
    "start_research": "submit_research",
    "research_status": "get_research",
    "save_research_note": "save_note",
    "list_research_notes": "list_notes",
    "read_research_note": "get_note",
    "export_research": "export_research",
    "draft_rule_from_research": "draft_rule_from_research",
    "get_knowledge_graph": "provenance_graph",
    "get_semantic_network": "semantic_network",
    "refresh_concept_similarity": "enqueue_similarity",
    "create_semantic_concept": "create_concept",
    "create_semantic_relation": "create_relation",
    "review_semantic_relation": "review_semantic",
}

# Handlers in the scope table that no MCP tool calls. They serve HTTP
# operations, so parity is checked against the declared operation instead.
HTTP_ONLY_HANDLERS: dict[str, str] = {
    "mark_applied": "uses.applied",
    "submit_processing": "sources.process",
}

# Administrative routes are not product operations, so they are not registered.
# They are the interesting case for prefix parity: the scope rules that hold
# them to kb:admin match on a literal /api/... prefix, which is precisely what a
# second prefix could have bypassed.
ADMIN_PATHS: tuple[tuple[str, str], ...] = (
    ("GET", "/orgs/{organisation_id}/members"),
    ("PUT", "/libraries/{library_id}/grants"),
    ("POST", "/libraries/{library_id}/grants"),
    ("DELETE", "/libraries/{library_id}/grants/{principal_id}"),
    ("PUT", "/libraries/{library_id}/policy"),
    ("POST", "/orgs/{organisation_id}/groups"),
)


def _route(routers: tuple[APIRouter, ...], method: str, path: str) -> APIRoute:
    for router in routers:
        for candidate in router.routes:
            if isinstance(candidate, APIRoute) and candidate.path == path:
                if method in (candidate.methods or ()):
                    return candidate
    raise AssertionError(f"no route {method} {path}")


def _query_field(route: APIRoute, name: str):
    for field in route.dependant.query_params:
        if field.name == name:
            return field
    raise AssertionError(f"{route.path} has no query parameter {name!r}")


def _bounds(field) -> tuple[int | None, int | None]:
    low = high = None
    for constraint in field.field_info.metadata:
        low = getattr(constraint, "ge", low)
        high = getattr(constraint, "le", high)
    return low, high


# --------------------------------------------------------------------- shape


def test_registry_is_self_consistent():
    assert_registry_consistent()


def test_contract_revision_is_declared():
    assert CONTRACT_REVISION == "architecture-20261015-v3"


def test_operation_names_are_unique():
    names = [operation.name for operation in OPERATIONS]
    assert len(names) == len(set(names))


def test_routes_are_unique_per_prefix():
    keys = [operation.route_key for operation in OPERATIONS]
    assert len(keys) == len(set(keys))


# ----------------------------------------------------------------- MCP bound


def test_every_mcp_tool_declares_exactly_one_operation():
    assert set(MCP_OPERATIONS) == set(EXPECTED_TOOLS)


def test_mcp_tool_names_are_unique():
    tools = [op.mcp_tool for op in OPERATIONS if op.mcp_tool is not None]
    assert len(tools) == len(set(tools))


def test_no_mcp_tool_is_bound_twice():
    for tool, operation in MCP_OPERATIONS.items():
        owners = [op for op in OPERATIONS if op.mcp_tool == tool]
        assert owners == [operation]


def test_identity_injection_guard_still_passes():
    assert_no_identity_injection()


# ------------------------------------------------------- router and registry


def test_every_registered_operation_exists_on_a_real_route():
    served = inventory()
    missing = [
        f"{operation.method} {operation.path}"
        for operation in OPERATIONS
        if operation.route_key not in served
    ]
    assert missing == []


def test_no_served_route_is_left_unclaimed():
    """Every served route under the API prefixes declares its scope.

    This used to print its result rather than assert it, and it was also
    comparing full canonical paths against prefix-free keys, so it reported
    every served route as unclaimed and could not notice a new one. The
    registry now covers the whole protected surface, so the residual is empty
    and an empty residual is a property worth failing on.
    """
    residual = unclaimed_routes({(m, f"{CANONICAL_PREFIX}{p}") for m, p in inventory()})
    assert residual == []


def test_scope_comes_from_the_registered_operation():
    """Dispatch-time scope is the registry's, and it agrees with the frozen table.

    The registry is the single declaration; ``http_scope`` is the legacy
    pattern table that predates it. Asserting they agree for all 83 served
    routes is what turns "the registry is the source" into a checked claim
    rather than an assertion in a comment.
    """
    disagreements = [
        f"{method} {path}: registry={scope_for_request(method, path)!r} "
        f"pattern={http_scope(method, path)!r}"
        for method, path in sorted(inventory())
        if scope_for_request(method, path) != http_scope(method, path)
    ]
    assert disagreements == []


def test_an_unclaimed_protected_path_is_closed():
    """A protected path no operation claims is denied, not pattern-matched."""
    assert scope_for_request("GET", "/api/v1/libraries/x/not-a-real-route") is None
    assert scope_for_request("GET", "/api/libraries/x/not-a-real-route") is None
    # Outside the API prefixes nothing is governed by the table, so the frozen
    # pattern answer is kept and the auth and static routes behave as before.
    assert scope_for_request("GET", "/auth/session") == "kb:read"


def test_read_and_write_effects_agree_with_the_method():
    for operation in OPERATIONS:
        if operation.effect is Effect.READ:
            assert operation.method == "GET", operation.name
        else:
            assert operation.method != "GET", operation.name


def test_operation_for_resolves_either_prefix():
    operation = operation_for("GET", "/libraries/{library_id}/sources")
    assert operation is not None and operation.name == "libraries.sources"
    alias = operation_for("GET", "/api/libraries/{library_id}/sources")
    assert alias is operation
    assert operation_for("GET", "/auth/session") is None


# ------------------------------------------------------------ alias parity


def test_both_prefixes_serve_every_operation_exactly_once():
    app = FastAPI()
    mount_api(app)
    counts = served_routes(app.router)
    assert all(count == 1 for count in counts.values()), [
        key for key, count in counts.items() if count > 1
    ]
    for operation in OPERATIONS:
        for prefix in API_PREFIXES:
            assert counts.get((operation.method, operation.full_path(prefix))) == 1, (
                f"{operation.name} under {prefix}"
            )


def test_alias_and_canonical_prefixes_agree_on_the_method():
    app = FastAPI()
    mount_api(app)
    counts = served_routes(app.router)
    for operation in OPERATIONS:
        by_prefix: dict[str, set[str]] = {}
        for method, path in counts:
            for prefix in API_PREFIXES:
                if path == operation.full_path(prefix):
                    by_prefix.setdefault(prefix, set()).add(method)
        assert len(by_prefix) == len(API_PREFIXES), (operation.name, sorted(by_prefix))
        # A path can host more than one operation (/libraries is both a list and
        # a create). The parity claim is that the prefixes offer the same verbs,
        # not that each offers only this operation's.
        assert {frozenset(found) for found in by_prefix.values()}.__len__() == 1, (
            operation.name,
            {key: sorted(value) for key, value in by_prefix.items()},
        )
        assert operation.method in by_prefix[CANONICAL_PREFIX]


def test_both_prefixes_require_the_same_scope():
    for operation in OPERATIONS:
        for prefix in API_PREFIXES:
            assert http_scope(operation.method, operation.full_path(prefix)) == operation.scope, (
                f"{operation.name} under {prefix}"
            )


def test_administrative_routes_keep_the_admin_scope_under_both_prefixes():
    for method, path in ADMIN_PATHS:
        scopes = {http_scope(method, f"{prefix}{path}") for prefix in API_PREFIXES}
        assert scopes == {"kb:admin"}, (method, path, scopes)


def test_canonical_api_path_collapses_both_prefixes():
    assert canonical_api_path("/api/libraries") == f"{CANONICAL_PREFIX}/libraries"
    assert canonical_api_path("/api/v1/libraries") == f"{CANONICAL_PREFIX}/libraries"
    assert canonical_api_path("/auth/session") is None
    # A bare prefix is returned canonical, not passed through. The old
    # assertion pinned the pass-through, which handed callers a string shorter
    # than the prefix they then sliced off (F-M3).
    assert canonical_api_path(f"{ALIAS_PREFIX}") == CANONICAL_PREFIX
    assert canonical_api_path(f"{CANONICAL_PREFIX}") == CANONICAL_PREFIX


def test_startup_assertions_reject_a_registry_that_drifted():
    app = FastAPI()
    mount_api(app)
    assert_prefix_parity(app)
    assert_routes_match_registry()


def test_the_schema_is_served_from_the_canonical_prefix_only():
    dual = FastAPI()
    mount_api(dual)
    canonical_only = FastAPI()
    mount_api(canonical_only, prefixes=(CANONICAL_PREFIX,))
    paths = dual.openapi()["paths"]
    assert paths, "the schema must not be empty"
    for path in paths:
        assert path.startswith(f"{CANONICAL_PREFIX}/"), path
    # The alias mount adds paths to the router and nothing to the schema, so a
    # client cannot see one operation twice under two operation ids.
    assert sorted(paths) == sorted(canonical_only.openapi()["paths"])


def test_the_alias_schema_matches_the_canonical_one():
    """The compatibility prefix is the same surface, path for path and verb for
    verb. Checked through the public OpenAPI surface rather than through the
    route objects, so it also proves a client sees the same thing in both."""
    canonical = FastAPI()
    mount_api(canonical, prefixes=(CANONICAL_PREFIX,))
    # The alias app is built by hand: mount_api keeps the alias out of the
    # schema on purpose, and this test needs to read the alias surface.
    alias = FastAPI()
    for router in ROUTERS:
        alias.include_router(router, prefix=ALIAS_PREFIX)

    def shape(app: FastAPI, prefix: str) -> set[tuple[str, str]]:
        return {
            (method, path[len(prefix) :])
            for path, methods in app.openapi()["paths"].items()
            for method in methods
        }

    assert shape(alias, ALIAS_PREFIX) == shape(canonical, CANONICAL_PREFIX)


# ------------------------------------------------------------------- scope


def test_declared_scope_matches_the_handler_scope_table():
    for tool, operation in MCP_OPERATIONS.items():
        handler = TOOL_HANDLER[tool]
        assert HANDLER_SCOPES[handler] == operation.scope, (tool, handler)


def test_every_handler_in_the_scope_table_is_reachable():
    handled = set(TOOL_HANDLER.values()) | set(HTTP_ONLY_HANDLERS)
    unreachable = sorted(set(HANDLER_SCOPES) - handled)
    assert unreachable == []
    for handler, operation_name in HTTP_ONLY_HANDLERS.items():
        operation = OPERATIONS_BY_NAME[operation_name]
        assert HANDLER_SCOPES[handler] == operation.scope, (handler, operation_name)


# -------------------------------------------------------------- pagination


def test_sources_knowledge_and_rules_are_paginated():
    assert set(PAGINATED_OPERATIONS) == {
        "/libraries/{library_id}/sources",
        "/libraries/{library_id}/knowledge",
        "/libraries/{library_id}/rules",
    }


@pytest.mark.parametrize(
    "path",
    [
        "/libraries/{library_id}/sources",
        "/libraries/{library_id}/knowledge",
        "/libraries/{library_id}/rules",
    ],
)
def test_paginated_routes_expose_bounded_limit_and_offset(path):
    operation = operation_for("GET", f"{CANONICAL_PREFIX}{path}")
    assert operation is not None and operation.is_paginated
    page = operation.page
    assert page is not None
    route = _route(ROUTERS, "GET", path)
    limit = _query_field(route, "limit")
    offset = _query_field(route, "offset")
    assert limit.default == page.limit_default
    assert offset.default == page.offset_default
    assert _bounds(limit) == (1, page.limit_max)
    assert _bounds(offset) == (0, None)
    assert page.limit_default == page.limit_max


def test_paginated_routes_keep_a_list_response():
    for path in PAGINATED_OPERATIONS:
        route = _route(ROUTERS, "GET", path)
        # No response_model: the body stays the bare list the catalog returned,
        # so an existing client that reads rows does not need to unwrap an
        # envelope this change did not negotiate with it.
        assert route.response_model is None, path
        assert route.status_code in (None, 200), path


def test_the_aggregate_router_has_no_prefix_of_its_own():
    api = build_api_router()
    paths = {path for _method, path in served_routes(api)}
    assert "/libraries/{library_id}/sources" in paths
    assert not any(path.startswith("/api") for path in paths)


# Synthetic configuration for the one test that builds the real application.
# Nothing here is a secret and nothing authenticates against it: the pool is
# created closed, the lifespan never runs and no request is served. The point is
# to reach the wiring that only exists inside create_app.
REAL_APP_ENV: dict[str, str] = {
    "KB_ALLOWED_HOSTS": "gateway.example",
    "KB_DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",
    "KB_MCP_AUDIENCE": "mcp",
    "KB_MCP_CLIENT_ID": "mcp-client",
    "KB_MCP_RESOURCE_URL": "https://gateway.example/mcp",
    "KB_OIDC_ISSUER": "https://id.example/realms/kb",
    "KB_OIDC_AUDIENCE": "kb-web",
    "KB_OIDC_CLIENT_ID": "kb-web-client",
    "KB_OIDC_CLIENT_SECRET": "placeholder-not-a-secret",
    "KB_IDENTITY_KEY": base64.urlsafe_b64encode(b"\x00" * 32).decode(),
    "KB_OIDC_REDIRECT_URI": "https://gateway.example/auth/callback",
    "KB_OIDC_JWKS_URL": "https://id.example/jwks",
}


def test_the_real_application_serves_both_prefixes(monkeypatch, tmp_path):
    """The registry, the router and the factory have to agree in one place.

    Every other test in this file assembles routers. Only this one reaches
    create_app, which is where install_auth, mount_api and the static mount
    meet - and where a second prefix could have been mounted wrongly.
    """
    for key, value in REAL_APP_ENV.items():
        monkeypatch.setenv(key, value)
    # A per-test directory, not a hardcoded one: nothing is written to it in
    # this test, but the app would refuse to start without a blob root.
    monkeypatch.setenv("KB_BLOB_ROOT", str(tmp_path))

    app = create_app()
    assert_prefix_parity(app)
    counts = served_routes(app.router)

    product_routes = inventory()
    api_paths = {key for key in counts if key[1].startswith("/api/")}
    assert len(api_paths) == 2 * len(product_routes)
    assert {key for key in counts if key[1].startswith(f"{CANONICAL_PREFIX}/")} == {
        (method, f"{CANONICAL_PREFIX}{path}") for method, path in product_routes
    }
    # /api/v1 starts with /api, so the alias has to be selected by exclusion.
    alias_only = {
        key
        for key in counts
        if key[1].startswith(f"{ALIAS_PREFIX}/") and not key[1].startswith(f"{CANONICAL_PREFIX}/")
    }
    assert alias_only == {(method, f"{ALIAS_PREFIX}{path}") for method, path in product_routes}
    assert not [key for key, count in counts.items() if count > 1]
    # The identity surface and the static mount are untouched by either change.
    assert "/auth/session" in {path for _method, path in counts}
    assert any(getattr(route, "name", "") == "static" for route in app.routes)
