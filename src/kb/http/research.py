"""Browser adapters to the same private catalog used by authenticated MCP."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from kb.catalog import graphs
from kb.contracts.research_tools import (
    ConceptArgs,
    DraftRuleArgs,
    RelationArgs,
    ResearchArgs,
    ReviewArgs,
    SaveNoteArgs,
    SimilarityArgs,
    UpdateNoteArgs,
)
from kb.http.libraries import Db, Me, mapped_errors
from kb.research import notes, rule_handoff, runs, semantic, similarity


def no_store(response: Response):
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(tags=["research"], dependencies=[Depends(no_store)])


@router.get("/research/sources")
def sources(library_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return runs.list_research_sources(conn, me, library_id)


@router.get("/research")
def history(library_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return runs.list_research(conn, me, library_id)


@router.post("/research", status_code=202)
def start(body: ResearchArgs, conn: Db, me: Me):
    with mapped_errors():
        return runs.submit_research(conn, me, **body.model_dump())


@router.get("/research/{run_id}")
def status(run_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return runs.get_research(conn, me, run_id)


@router.get("/research/{run_id}/markdown")
def export(run_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return notes.export_research(conn, me, run_id)


@router.post("/research/{run_id}/draft-rule", status_code=201)
def draft(run_id: UUID, body: DraftRuleArgs, conn: Db, me: Me):
    if run_id != body.run_id:
        from fastapi import HTTPException

        raise HTTPException(400, "run_id_mismatch")
    with mapped_errors():
        return rule_handoff.draft_rule_from_research(conn, me, **body.model_dump())


@router.get("/notes")
def note_list(library_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return notes.list_notes(conn, me, library_id)


@router.post("/notes", status_code=201)
def note_save(body: SaveNoteArgs, conn: Db, me: Me):
    with mapped_errors():
        return notes.save_note(conn, me, **body.model_dump())


@router.get("/notes/{note_id}")
def note_read(note_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return notes.get_note(conn, me, note_id)


@router.patch("/notes/{note_id}")
def note_update(note_id: UUID, body: UpdateNoteArgs, conn: Db, me: Me):
    with mapped_errors():
        return notes.update_note(conn, me, note_id, **body.model_dump())


@router.get("/graphs/provenance")
def provenance(
    library_id: UUID,
    conn: Db,
    me: Me,
    source_ids: Annotated[list[UUID] | None, Query()] = None,
    limit: int = Query(default=200, ge=1, le=500),
):
    with mapped_errors():
        return graphs.provenance_graph(conn, me, library_id, source_ids, limit)


@router.get("/graphs/semantic")
def network(library_id: UUID, conn: Db, me: Me, limit: int = Query(default=200, ge=1, le=500)):
    with mapped_errors():
        return semantic.semantic_network(conn, me, library_id, limit)


@router.post("/graphs/similarity", status_code=202)
def similarity_refresh(body: SimilarityArgs, conn: Db, me: Me):
    with mapped_errors():
        return similarity.enqueue_similarity(conn, me, body.library_id)


@router.post("/graphs/concepts", status_code=201)
def concept(body: ConceptArgs, conn: Db, me: Me):
    with mapped_errors():
        return semantic.create_concept(conn, me, **body.model_dump())


@router.post("/graphs/relations", status_code=201)
def relation(body: RelationArgs, conn: Db, me: Me):
    with mapped_errors():
        return semantic.create_relation(conn, me, **body.model_dump())


@router.post("/graphs/review")
def review(body: ReviewArgs, conn: Db, me: Me):
    with mapped_errors():
        return semantic.review_semantic(conn, me, **body.model_dump())
