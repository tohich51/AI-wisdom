"""One transport, two prefixes, and a startup proof that they agree.

``/api`` shipped first and the browser still calls it, so it stays. ``/api/v1``
is the canonical prefix and the one the OpenAPI schema is served from. Both
mount the *same* router object, so an operation is declared once and a
compatibility prefix cannot drift into a second implementation of it.

The alias adds paths. It does not add work, and
:func:`assert_prefix_parity` is what makes that checkable: it fails if any
``(method, full path)`` pair is bound more than once, which is the structural
form of "a POST through the alias is executed once, not twice". It is a
**test-time** assertion, called from ``tests/runtime/test_operation_registry.py``
and nowhere on the startup path — this docstring used to claim otherwise, and
the two statements in this repository contradicted each other.

What *is* a startup error is the neighbouring check: every registered operation
is matched against the real route table by
:func:`assert_routes_match_registry`, which :func:`mount_api` calls while the
application is being built. A registry entry naming a route nobody serves is
therefore a startup error, not a contract that quietly documents a fiction.
The two checks answer different questions and are called from different places,
so neither is described as the other.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from kb.contracts.operations import (
    ALIAS_PREFIX,
    API_PREFIXES,
    CANONICAL_PREFIX,
    OPERATION_ROUTES,
    Operation,
    assert_registry_consistent,
)

# Import order matters only for readability, but the set of routers is the
# route inventory: it was the same six routers mounted under /api before v1.
from kb.http import browser_pairing, content, libraries, members, projects, research, uploads

ROUTERS: tuple[APIRouter, ...] = (
    browser_pairing.approval_router,
    libraries.router,
    projects.router,
    members.router,
    uploads.router,
    content.router,
    research.router,
)


def build_api_router() -> APIRouter:
    """Aggregate the product routers, with no prefix applied."""
    assert_registry_consistent()
    api = APIRouter()
    for router in ROUTERS:
        api.include_router(router)
    return api


def mount_api(app: FastAPI, prefixes: tuple[str, ...] = API_PREFIXES) -> APIRouter:
    """Mount the product API under every supported prefix.

    The canonical prefix stays in the schema; the alias does not, because two
    copies of a route produce two operation ids for one operation.
    """
    api = build_api_router()
    for prefix in prefixes:
        app.include_router(
            api,
            prefix=prefix,
            include_in_schema=prefix == CANONICAL_PREFIX,
        )
    assert_routes_match_registry()
    return api


def inventory(routers: tuple[APIRouter, ...] = ROUTERS) -> set[tuple[str, str]]:
    """``(method, prefix-free path)`` for every product route actually served."""
    found: set[tuple[str, str]] = set()
    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods or ():
                if method in {"HEAD", "OPTIONS"}:
                    continue
                found.add((method, route.path))
    return found


def served_routes(node: object) -> dict[tuple[str, str], int]:
    """``(method, full path) -> times bound`` for an app or a router.

    Pass ``app.router`` for an application, or a bare router. FastAPI 0.143
    keeps an included router as a nested node instead of flattening it into the
    parent's route list, so the leaves are reached through ``original_router``
    and the prefix recorded on the include context. Both are read by attribute
    rather than by importing a private class, so a FastAPI that flattens again
    keeps working: in that case the leaves are found directly.

    The same aggregate router is deliberately included under both prefixes, so
    the guard against a cycle is per branch. A global set of visited objects
    would drop the second mount and report the alias as unserved.
    """
    counts: dict[tuple[str, str], int] = {}

    def walk(node: object, prefix: str, ancestors: frozenset[int]) -> None:
        if id(node) in ancestors:
            return
        nested = ancestors | {id(node)}
        original = getattr(node, "original_router", None)
        if original is not None:
            context = getattr(node, "include_context", None)
            walk(original, prefix + (getattr(context, "prefix", "") or ""), nested)
            return
        for route in getattr(node, "routes", ()) or ():
            if isinstance(route, APIRoute):
                for method in route.methods or ():
                    if method in {"HEAD", "OPTIONS"}:
                        continue
                    key = (method, prefix + route.path)
                    counts[key] = counts.get(key, 0) + 1
            else:
                walk(route, prefix, nested)

    walk(node, "", frozenset())
    return counts


def assert_prefix_parity(app: FastAPI, prefixes: tuple[str, ...] = API_PREFIXES) -> None:
    """Both prefixes serve every operation once, with no duplicated binding.

    A duplicate ``(method, path)`` is not cosmetic. It means two route objects
    can answer the same request, and for a write that is two chances to run the
    effect. Refuse to start instead.
    """
    counts = served_routes(app.router)
    duplicated = sorted(key for key, count in counts.items() if count > 1)
    if duplicated:
        raise ValueError(f"a route is bound twice; a write could run twice: {duplicated}")
    for operation in OPERATION_ROUTES.values():
        for prefix in prefixes:
            if counts.get((operation.method, operation.full_path(prefix))) != 1:
                raise ValueError(
                    f"{operation.name} is not served exactly once under {prefix}: "
                    f"{operation.method} {operation.full_path(prefix)}"
                )


def assert_routes_match_registry() -> None:
    """Every declared operation resolves to a route that the app serves."""
    served = inventory()
    missing = sorted(
        f"{operation.method} {operation.path}"
        for operation in OPERATION_ROUTES.values()
        if operation.route_key not in served
    )
    if missing:
        raise ValueError(f"the registry declares routes that do not exist: {missing}")


def registry_operations() -> tuple[Operation, ...]:
    return tuple(OPERATION_ROUTES.values())


__all__ = [
    "ALIAS_PREFIX",
    "API_PREFIXES",
    "CANONICAL_PREFIX",
    "ROUTERS",
    "assert_prefix_parity",
    "assert_routes_match_registry",
    "build_api_router",
    "inventory",
    "mount_api",
    "registry_operations",
    "served_routes",
]
