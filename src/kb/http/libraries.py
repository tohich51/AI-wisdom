"""C09 — HTTP surface for the library catalogue.

The routers here are the adapter; PostgreSQL is still the authority. Every
handler is a thin translation of a catalogue call, and the access decision is
made by RLS on the connection, not by anything in this file.

**Where identity comes from.** ``current_principal`` reads
``request.state.principal``, which the authentication middleware sets after it
has verified the Keycloak token. That middleware is C07's artifact and is not
re-implemented here. This module has no header, query parameter, cookie or body
field that can set a principal, and every request model is ``extra="forbid"``,
so a body carrying ``user_id`` or ``role`` is rejected with 422 before it
reaches a handler. A ``Principal`` instance is checked for on the way out too:
if anything puts a string in ``request.state.principal`` by mistake, the router
refuses it rather than passing a truthy stranger down to a query.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from psycopg_pool import ConnectionPool

from kb.access.policy import AccessDenied, Principal
from kb.catalog import libraries as catalog
from kb.contracts.enums import LibraryKind

router = APIRouter(tags=["libraries"])


# ----------------------------------------------------------- dependencies


def db_connection(request: Request) -> Iterator[psycopg.Connection]:
    """A pooled connection, checked out for the length of one request.

    The pool lives on ``app.state`` because building and warming it is the
    gateway's job, not a route's. A router that opened its own connection per
    request would also make it easy to forget the transaction-local identity.
    """
    pool: ConnectionPool | None = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database pool is not configured")
    with pool.connection() as conn:
        yield conn


def current_principal(request: Request) -> Principal:
    """Identity from the transport, or nothing at all.

    There is no fallback. No header, no query string, no body. A request that
    arrives without a verified principal gets 401 before any SQL runs, which is
    the same default deny the database would give it — earlier, and cheaper.
    """
    principal = getattr(request.state, "principal", None)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no authenticated principal")
    if not isinstance(principal, Principal):
        # A string in request.state.principal is a bug in the auth layer, not
        # an identity. Refuse it instead of trusting its shape.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "malformed principal")
    return principal


Db = Annotated[psycopg.Connection, Depends(db_connection)]
Me = Annotated[Principal, Depends(current_principal)]


@contextlib.contextmanager
def mapped_errors():
    """Map the catalogue's refusals and the server's constraint errors.

    ``AccessDenied`` always says "forbidden" and never distinguishes
    "does not exist" from "not yours" — the difference would be a directory of
    invisible objects.

    The psycopg mappings matter for a different reason: a bad reference in a
    request body is *caller* error, and answering it with a 500 teaches
    operators to ignore 500s. A foreign key that fails here has already passed
    the WITH CHECK that requires curator on the rule's own library, so naming
    the constraint leaks nothing about an object the caller could not see.
    """
    try:
        yield
    except AccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except psycopg.errors.InsufficientPrivilege as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "referenced object does not exist or is not visible"
        ) from exc
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "that record already exists") from exc
    except psycopg.errors.CheckViolation as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "the request violates a stored rule"
        ) from exc


# ------------------------------------------------------------- type registry


@router.get("/library-types")
def list_types(
    conn: Db,
    me: Me,
    include_retired: Annotated[bool, Query()] = False,
) -> list[catalog.LibraryType]:
    """The type catalogue.

    A vocabulary, not user content: keys, titles, templates and review
    processes. Listing it says nothing about which libraries anybody has.
    """
    with mapped_errors():
        return catalog.list_library_types(conn, me, include_retired=include_retired)


@router.post("/library-types", status_code=status.HTTP_201_CREATED)
def register_type(
    body: catalog.RegisterLibraryType, conn: Db, me: Me, response: Response
) -> catalog.LibraryType:
    """Add a type to the registry — configuration, not content.

    409 on an existing key rather than a silent overwrite: a re-run of the same
    configuration must be a no-op, and quietly replacing a core type's template
    would be worse than a visible conflict.
    """
    with mapped_errors():
        created = catalog.register_library_type(conn, me, body)
    if created is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "type key already registered")
    return created


# ----------------------------------------------------------------- libraries


@router.get("/libraries")
def list_libs(
    conn: Db,
    me: Me,
    kind: Annotated[list[LibraryKind] | None, Query()] = None,
    audience_scope: Annotated[catalog.AudienceScope | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> catalog.LibraryPage:
    """Libraries the caller can see, filtered and counted server-side.

    RLS has already removed the rest. ``kind`` and ``audience_scope`` narrow the
    visible set and cannot extend it, and ``total`` is counted over the same
    filtered statement — a global count would be a map of libraries the caller
    is not allowed to know exist.
    """
    with mapped_errors():
        return catalog.list_libraries(
            conn, me, kinds=kind, audience_scope=audience_scope, limit=limit, offset=offset
        )


@router.post("/libraries", status_code=status.HTTP_201_CREATED)
def create_lib(body: catalog.CreateLibrary, conn: Db, me: Me) -> catalog.Library:
    with mapped_errors():
        return catalog.create_library(conn, me, body)


@router.get("/libraries/{library_id}")
def read_lib(library_id: UUID, conn: Db, me: Me) -> catalog.Library:
    """404 for a library you may not open, exactly as for one that is not there."""
    with mapped_errors():
        found = catalog.get_library(conn, me, library_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such library")
    return found


# -------------------------------------------------------------------- grants


@router.get("/libraries/{library_id}/grants")
def read_grants(library_id: UUID, conn: Db, me: Me) -> list[catalog.Grant]:
    """Per-library roles. You see yours; a manager sees the whole set.

    Grants are per library and never inherited. Being a manager of a project
    that links a library does not put you in this list, and a role here never
    reaches any other library.
    """
    with mapped_errors():
        return catalog.list_grants(conn, me, library_id)


@router.put("/libraries/{library_id}/grants")
def put_grant(library_id: UUID, body: catalog.SetGrant, conn: Db, me: Me) -> catalog.Grant:
    """Set one principal's role. Manager only — enforced by RLS.

    A refused write is a 403, never a 200. An UPDATE under RLS is filtered and
    reports a zero row count without raising, so "no error" is not evidence that
    anything happened.
    """
    with mapped_errors():
        return catalog.set_grant(conn, me, library_id, body)


@router.delete("/libraries/{library_id}/grants/{principal_id}", status_code=204)
def drop_grant(library_id: UUID, principal_id: UUID, conn: Db, me: Me) -> Response:
    with mapped_errors():
        catalog.revoke_grant(conn, me, library_id, principal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# A named alias so the projects router can import the shared dependencies from
# one place instead of the other. There is no src/kb/http/deps.py: this card's
# allowed paths are src/kb/http/libraries* and src/kb/http/projects*.
__all__ = [
    "Db",
    "Me",
    "current_principal",
    "db_connection",
    "mapped_errors",
    "router",
]
