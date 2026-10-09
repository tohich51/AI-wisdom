"""C09 — the library catalogue: types, libraries, per-library grants.

Two responsibilities that look like one and are not:

* the **type registry** (``kb.library_type``) — an extensible catalogue of what
  a library *is*. A type is data, so it grows by INSERT, without DDL and
  without touching a single already-loaded source.
* the **library** and its **grants** — who may see a specific library.

What this module deliberately does *not* do: decide access. PostgreSQL is the
authority (see ``kb.access.policy`` and ``migrations/0002_rls.sql``). Every
query here runs as the caller's role with the caller's transaction-local
principal, so RLS has already removed the rows the caller may not see by the
time a row reaches Python. Filters in this module only ever *narrow* what RLS
left; none of them widen it. A count is taken over the same filtered, RLS-scoped
relation for the same reason — a count is a disclosure channel (A20), and a
total taken from ``kb.library`` without a policy would be a directory of
invisible objects.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.contracts.entities import Grant, Library
from kb.contracts.enums import LibraryKind, LibraryRole

AudienceScope = Literal["private", "invited", "all_invited"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ======================================================== type registry


class LibraryType(_Model):
    """One entry of the type registry.

    ``description`` is nullable because a type genuinely may have none. It is
    never filled with "n/a" or a generated sentence — that is the same
    fabricated provenance the contracts forbid elsewhere.
    """

    key: str
    title: str
    description: str | None = None
    template_version: int = Field(ge=1)
    allowed_extra_fields: list[dict[str, Any]] = Field(default_factory=list)
    review_process: dict[str, Any] = Field(default_factory=dict)
    is_core: bool = False
    retired_at: dt.datetime | None = None


class RegisterLibraryType(_Model):
    """Configuration write.

    No principal, no role, no audience: a request model must never carry a
    caller-asserted identity or authority (C02 MCP contract, hard rule 4).
    """

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    allowed_extra_fields: list[dict[str, Any]] = Field(default_factory=list)
    review_process: dict[str, Any] = Field(default_factory=dict)
    template_version: int = Field(default=1, ge=1)


_TYPE_COLUMNS = (
    "t.key, t.title, t.description, t.template_version, "
    "t.allowed_extra_fields, t.review_process, t.is_core, t.retired_at"
)

_LIB_COLUMNS = "l.id, l.name, l.kind, l.audience_scope, l.created_at, l.generation"


def _type_from_row(row: tuple) -> LibraryType:
    return LibraryType(
        key=row[0],
        title=row[1],
        description=row[2],
        template_version=row[3],
        allowed_extra_fields=list(row[4] or []),
        review_process=dict(row[5] or {}),
        is_core=row[6],
        retired_at=row[7],
    )


def _library_from_row(row: tuple) -> Library:
    return Library(
        id=row[0],
        name=row[1],
        kind=LibraryKind(row[2]),
        audience_scope=row[3],
        created_at=row[4],
        generation=row[5],
    )


def list_library_types(
    conn: psycopg.Connection,
    principal: Principal | None = None,
    *,
    include_retired: bool = False,
) -> list[LibraryType]:
    """The registry, core types first and stable after them.

    Retired types are excluded by default: a retired type is no longer offered
    for new libraries, but it stays resolvable for the libraries already
    pointing at it. Deleting the row instead would break them.

    ``principal`` is optional only so that the migration/bootstrap path can read
    the catalogue before anyone has logged in. Every request-scoped caller must
    pass it: ``kb.library_type_read`` is ``USING (current_principal() IS NOT
    NULL)``, so a read without a transaction-local identity returns nothing.
    """
    query = sql.SQL("SELECT {} FROM kb.library_type t").format(sql.SQL(_TYPE_COLUMNS))
    if not include_retired:
        query += sql.SQL(" WHERE t.retired_at IS NULL")
    query += sql.SQL(" ORDER BY t.is_core DESC, t.key")
    with _as(conn, principal) as cur:
        cur.execute(query)
        return [_type_from_row(r) for r in cur.fetchall()]


def get_library_type(
    conn: psycopg.Connection, key: str, principal: Principal | None = None
) -> LibraryType | None:
    query = sql.SQL("SELECT {} FROM kb.library_type t WHERE t.key = %s").format(
        sql.SQL(_TYPE_COLUMNS)
    )
    with _as(conn, principal) as cur:
        cur.execute(query, (key,))
        row = cur.fetchone()
    return _type_from_row(row) if row else None


@contextlib.contextmanager
def _as(conn: psycopg.Connection, principal: Principal | None):
    """Run a statement as ``principal``; with no principal, as nobody.

    The registry read policy needs an identity, so a request-scoped read passes
    one. A caller that passes None gets the default-deny answer rather than an
    exception — which is the correct result for "no identity", just not a useful
    one for a live request.
    """
    if principal is None:
        with conn.cursor() as cur:
            yield cur
        return
    with transaction_identity(conn, principal), conn.cursor() as cur:
        yield cur


def register_library_type(
    conn: psycopg.Connection, principal: Principal, spec: RegisterLibraryType
) -> LibraryType | None:
    """Add a type to the registry. Returns None when the key already exists.

    A duplicate is reported rather than raised, so re-running a configuration
    script is safe; silently overwriting a core type's template would be worse
    than doing nothing.
    """
    if get_library_type(conn, spec.key, principal) is not None:
        return None
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb.library_type
                (key, title, description, template_version,
                 allowed_extra_fields, review_process, is_core)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, false)
            """,
            (
                spec.key,
                spec.title,
                spec.description,
                spec.template_version,
                json.dumps(spec.allowed_extra_fields),
                json.dumps(spec.review_process),
            ),
        )
    created = get_library_type(conn, spec.key, principal)
    if created is None:  # pragma: no cover - the insert above either lands or raises
        raise RuntimeError("library type vanished immediately after insert")
    return created


# ============================================================= libraries


class LibraryPage(_Model):
    """A page of libraries *and* the total over the same RLS-scoped set.

    ``total`` comes from the identical filtered statement, not from a second
    unrestricted query. A global count would tell a caller how many libraries
    exist that they cannot see (A20).
    """

    libraries: list[Library]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class CreateLibrary(_Model):
    organisation_id: UUID
    name: str = Field(min_length=1, max_length=200)
    kind: LibraryKind
    # Independent axis: no validator ties it to ``kind``, and none should be
    # added. A brand library may be private; a reference library may be
    # readable by everyone invited. Coupling them is the bug this card exists
    # to prevent, not a validation rule.
    audience_scope: AudienceScope = "private"


def list_libraries(
    conn: psycopg.Connection,
    principal: Principal,
    *,
    kinds: list[LibraryKind] | None = None,
    audience_scope: AudienceScope | None = None,
    limit: int = 50,
    offset: int = 0,
) -> LibraryPage:
    """Libraries the caller can see, filtered and paginated server-side.

    RLS has already dropped the rest. The predicates below only narrow: a
    client filter can reduce the visible set, never extend it.
    """
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must not be negative")

    where: list[sql.SQL] = []
    params: list[Any] = []
    if kinds:
        where.append(sql.SQL("l.kind = ANY(%s)"))
        params.append([k.value for k in kinds])
    if audience_scope is not None:
        where.append(sql.SQL("l.audience_scope = %s"))
        params.append(audience_scope)
    tail = sql.SQL(" AND ").join(where) if where else sql.SQL("")

    query: sql.Composable = sql.SQL(
        "SELECT {} , count(*) OVER () AS visible_total FROM kb.library l"
    ).format(sql.SQL(_LIB_COLUMNS))
    if where:
        query += sql.SQL(" WHERE ") + tail
    query += sql.SQL(" ORDER BY l.created_at, l.id LIMIT %s OFFSET %s")
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(query, [*params, limit, offset])
        rows = cur.fetchall()

    if rows:
        total = int(rows[0][6])
    else:
        # An empty page past the end has no row to carry the total. Ask for it
        # over the same clause rather than reporting 0: "no libraries" and
        # "no libraries on this page" are different answers, and only one of
        # them is true.
        count_query: sql.SQL | sql.Composed = sql.SQL("SELECT count(*) FROM kb.library l")
        if where:
            count_query += sql.SQL(" WHERE ") + tail
        with transaction_identity(conn, principal), conn.cursor() as cur:
            cur.execute(count_query, params)
            counted = cur.fetchone()
        if counted is None:  # pragma: no cover - count(*) always returns a row
            raise RuntimeError("count query returned no row")
        total = int(counted[0])
    return LibraryPage(
        libraries=[_library_from_row(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


def get_library(conn: psycopg.Connection, principal: Principal, library_id: UUID) -> Library | None:
    """None means "not visible to you" — which is also what "does not exist"
    means here. Collapsing the two is not politeness: the difference is a
    directory of objects the caller is not allowed to know about."""
    query = sql.SQL("SELECT {} FROM kb.library l WHERE l.id = %s").format(sql.SQL(_LIB_COLUMNS))
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(query, (library_id,))
        row = cur.fetchone()
    return _library_from_row(row) if row else None


def create_library(conn: psycopg.Connection, principal: Principal, spec: CreateLibrary) -> Library:
    """Create a library and make its creator its manager.

    The manager grant is not a convenience. Without it the creator could not
    read back the library RLS has just let them create, and the caller would be
    handed a reference to something they cannot open.

    It goes through ``kb.create_owned_library`` rather than two plain INSERTs
    because 0002's grant policy cannot express the first one: a library that
    does not exist yet has no effective role for anybody, so the runtime role is
    refused. The function is SECURITY DEFINER for that single bootstrap and
    nothing else, and it forces the principal to be the authenticated caller.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT kb.create_owned_library(%s, %s, %s::kb.library_kind, %s, %s)",
            (
                spec.organisation_id,
                spec.name,
                spec.kind.value,
                spec.audience_scope,
                principal.principal_id,
            ),
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover - the function always returns a row
        raise RuntimeError("create_owned_library returned no id")
    created = get_library(conn, principal, row[0])
    if created is None:  # pragma: no cover
        raise RuntimeError("library is invisible to its own creator immediately after create")
    return created


# ================================================================ grants


class SetGrant(_Model):
    principal_id: UUID
    role: LibraryRole


def list_grants(conn: psycopg.Connection, principal: Principal, library_id: UUID) -> list[Grant]:
    """Grants on one library, subject to the same RLS as everything else.

    A principal sees their own row; a manager sees the whole set. A reader gets
    their own single row and nothing else — enough for the UI to say "you are a
    reader here" without turning the endpoint into a roster of who can see
    what.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "SELECT library_id, principal_id, role FROM kb.library_grant "
            "WHERE library_id = %s ORDER BY principal_id",
            (library_id,),
        )
        return [
            Grant(library_id=r[0], principal_id=r[1], role=LibraryRole(r[2]))
            for r in cur.fetchall()
        ]


def set_grant(
    conn: psycopg.Connection, principal: Principal, library_id: UUID, spec: SetGrant
) -> Grant:
    """Upsert one grant. Manager-only, enforced by RLS.

    An ``UPDATE`` under RLS is *filtered*, not rejected: without the right role
    it reports a zero row count and raises nothing. This function treats zero as
    failure, because the C06 negative tests show exactly how silent a filtered
    write is.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb.library_grant (library_id, principal_id, role)
            VALUES (%s, %s, %s)
            ON CONFLICT (library_id, principal_id)
            DO UPDATE SET role = EXCLUDED.role, granted_at = now()
            """,
            (library_id, spec.principal_id, spec.role.value),
        )
        if cur.rowcount == 0:  # pragma: no cover - a successful INSERT reports 1
            raise AccessDenied("forbidden")
    return Grant(library_id=library_id, principal_id=spec.principal_id, role=spec.role)


def revoke_grant(
    conn: psycopg.Connection, principal: Principal, library_id: UUID, target: UUID
) -> None:
    """Remove a grant. Absence raises: 'nothing was removed' is not success."""
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM kb.library_grant WHERE library_id = %s AND principal_id = %s",
            (library_id, target),
        )
        removed = cur.rowcount
    if removed == 0:
        raise AccessDenied("no such grant, or you may not manage it")
