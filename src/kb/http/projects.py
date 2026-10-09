"""C09 — HTTP surface for projects, their library links and their rule pins.

The route that carries the weight of this card is ``GET /projects/{id}/context``.

Linking a library to a project is a statement about requirements, not a
distribution of rights. So when a project member asks for their context and one
of the linked libraries is not theirs to open, the response says the context is
**blocked** and carries no id, no name, no kind and no count of the closed
library's contents. It does not silently shrink the context either: a member
who is told "your project is ready" while a mandatory brand rule was held back
has been given a worse answer than one who is told "not yet".

The same applies to pinned rules. A pin names an exact ``version_no``; the
database's foreign key makes "the current version of a rule" unpinnable, and the
policy makes pinning a rule you cannot read unreachable.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status

from kb.catalog import projects as catalog
from kb.http.libraries import Db, Me, mapped_errors

router = APIRouter(tags=["projects"])


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create(body: catalog.CreateProject, conn: Db, me: Me) -> catalog.ProjectDetail:
    """Create a project: a library of kind ``project`` plus its manifest."""
    with mapped_errors():
        return catalog.create_project(conn, me, body)


@router.get("/projects/{project_id}")
def read(project_id: UUID, conn: Db, me: Me) -> catalog.ProjectDetail:
    """Project metadata and manifest sizes. 404 when not visible to you."""
    with mapped_errors():
        found = catalog.get_project(conn, me, project_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such project")
    return found


@router.post("/projects/{project_id}/libraries", status_code=status.HTTP_204_NO_CONTENT)
def link(project_id: UUID, body: catalog.LinkLibrary, conn: Db, me: Me) -> Response:
    """Record that this project requires that library.

    Returns no body on purpose. The response cannot be mistaken for a handle:
    a 204 here means "recorded", and reading anything from the linked library
    still requires a grant that this call did not create.
    """
    with mapped_errors():
        catalog.link_library(conn, me, project_id, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/projects/{project_id}/libraries/{library_id}", status_code=status.HTTP_204_NO_CONTENT
)
def unlink(project_id: UUID, library_id: UUID, conn: Db, me: Me) -> Response:
    with mapped_errors():
        catalog.unlink_library(conn, me, project_id, library_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/projects/{project_id}/rules", status_code=status.HTTP_204_NO_CONTENT)
def pin(project_id: UUID, body: catalog.PinRule, conn: Db, me: Me) -> Response:
    """Pin one exact rule version as a project requirement.

    Needs curator on the project *and* curator on the rule's library: you cannot
    make a mandatory rule out of one you may not read.
    """
    with mapped_errors():
        catalog.pin_rule(conn, me, project_id, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/projects/{project_id}/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def unpin(project_id: UUID, rule_id: UUID, conn: Db, me: Me) -> Response:
    with mapped_errors():
        catalog.unpin_rule(conn, me, project_id, rule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}/context")
def context(project_id: UUID, conn: Db, me: Me) -> catalog.ProjectContext:
    """The project's mandatory context, as far as this caller may see it.

    ``context_state`` is ``complete`` only when every link and every pin
    resolved. A blocked requirement is reported as blocked, with nothing about
    the object behind it.
    """
    with mapped_errors():
        ctx = catalog.project_context(conn, me, project_id)
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such project")
    return ctx
