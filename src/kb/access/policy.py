"""C06 — the one access check.

The UI and the MCP endpoint both call this. PostgreSQL remains the authority:
this module does not *decide* access, it expresses the same rule in Python so
an application can fail early with a clear error instead of relying on a row
count of zero to tell the user they are forbidden.

If this module and the SQL policies ever disagree, the SQL wins and the
mismatch shows up as denied rows rather than as a permission granted by
accident. That is the safe direction to fail in.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from uuid import UUID

from kb.contracts.enums import LibraryRole

ROLE_RANK: dict[LibraryRole, int] = {
    LibraryRole.READER: 10,
    LibraryRole.CONTRIBUTOR: 20,
    LibraryRole.CURATOR: 30,
    LibraryRole.MANAGER: 40,
}


class AccessDenied(PermissionError):
    """Raised when a principal asks for something their role does not cover.

    The message never reveals whether the object exists. "Forbidden" and
    "does not exist" are the same answer to a caller, because the difference
    is a directory of invisible objects.
    """


@dataclass(frozen=True)
class Principal:
    principal_id: UUID
    account_id: UUID
    generation_watermark: int = 1


def effective_role(grants: dict[UUID, LibraryRole], library_id: UUID) -> LibraryRole | None:
    """Highest role held on one library, or None."""
    role = grants.get(library_id)
    return role


def authorize(
    principal: Principal,
    library_id: UUID,
    grants: dict[UUID, LibraryRole],
    *,
    need: LibraryRole,
) -> None:
    """Raise AccessDenied unless the principal holds at least `need`."""
    held = effective_role(grants, library_id)
    if held is None or ROLE_RANK[held] < ROLE_RANK[need]:
        raise AccessDenied("forbidden")
    if principal.generation_watermark < 1:
        raise AccessDenied("forbidden")


def can_read(principal: Principal, library_id: UUID, grants: dict[UUID, LibraryRole]) -> bool:
    return _holds(principal, library_id, grants, LibraryRole.READER)


def can_contribute(principal: Principal, library_id: UUID, grants: dict[UUID, LibraryRole]) -> bool:
    return _holds(principal, library_id, grants, LibraryRole.CONTRIBUTOR)


def can_curate(principal: Principal, library_id: UUID, grants: dict[UUID, LibraryRole]) -> bool:
    return _holds(principal, library_id, grants, LibraryRole.CURATOR)


def can_manage(principal: Principal, library_id: UUID, grants: dict[UUID, LibraryRole]) -> bool:
    return _holds(principal, library_id, grants, LibraryRole.MANAGER)


def _holds(
    principal: Principal, library_id: UUID, grants: dict[UUID, LibraryRole], need: LibraryRole
) -> bool:
    if principal.generation_watermark < 1:
        return False
    held = grants.get(library_id)
    return held is not None and ROLE_RANK[held] >= ROLE_RANK[need]


# ------------------------------------------------------------ identity


@contextlib.contextmanager
def transaction_identity(conn, principal: Principal):
    """Set transaction-local identity for the duration of the block.

    ``SET LOCAL`` scopes the GUC to the transaction, so the identity cannot
    outlive it and cannot leak into the next request on a pooled connection.
    The GUC name avoids reserved words on purpose: `app.user` is a syntax
    error, which C00 recorded against the real server.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.principal', %s, true)", (str(principal.principal_id),)
            )
            cur.execute("SELECT set_config('app.account', %s, true)", (str(principal.account_id),))
        yield conn


def clear_identity(conn) -> None:
    """Drop any identity left on a connection. Call before returning to a pool.

    A connection that keeps a principal is a connection that answers the next
    caller's request as the previous caller.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.principal', '', false)")
        cur.execute("SELECT set_config('app.account', '', false)")
