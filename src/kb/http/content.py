"""Thin HTTP adapters sharing the catalog and SQL access policies with MCP."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from kb.access import membership
from kb.catalog import content, policies, shared_experience, workspace, writes
from kb.http.libraries import Db, Me, mapped_errors
from kb.jobs.ingestion import submit_processing

router = APIRouter(tags=["knowledge"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProcessInput(Input):
    idempotency_key: str = Field(min_length=8, max_length=200)
    extract: bool = False


class UseInput(Input):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    rule_id: UUID
    version_no: int = Field(ge=1)
    expected_result: str | None = Field(default=None, max_length=4000)
    where_applied: str | None = Field(default=None, max_length=2000)


class OutcomeInput(Input):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    observed_result: str | None = Field(default=None, max_length=4000)
    rating: int | None = Field(default=None, ge=1, le=5)
    rating_basis: str | None = Field(default=None, max_length=2000)
    achievement: Literal["met", "partly", "not_met", "unknown"] | None = None
    assessor_kind: Literal["human", "agent", "measurement", "unknown"] = "unknown"
    assessment_evidence: str | None = Field(default=None, max_length=4000)
    is_correction: bool = False


class MetadataInput(Input):
    author: str | None = Field(default=None, max_length=500)
    edition: str | None = Field(default=None, max_length=500)
    publication_ref: str | None = Field(default=None, max_length=2000)


class ExportInput(Input):
    rule_ids: list[UUID] = Field(min_length=1, max_length=100)


class PublishExperienceInput(Input):
    use_id: UUID
    outcome_id: UUID


@router.get("/me")
def me_route(conn: Db, me: Me):
    with mapped_errors():
        return {
            "principal_id": me.principal_id,
            "account_id": me.account_id,
            # Additive hint for UI gating only; the SQL function decides.
            "is_org_admin": membership.is_org_admin(conn, me, me.account_id),
        }


@router.get("/libraries/{library_id}/policy")
def library_policy(library_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return policies.get_policy(conn, me, library_id)


@router.put("/libraries/{library_id}/policy")
def update_library_policy(library_id: UUID, body: policies.LibraryPolicyInput, conn: Db, me: Me):
    with mapped_errors():
        return policies.put_policy(conn, me, library_id, body)


@router.get("/libraries/{library_id}/sources")
def sources(
    library_id: UUID,
    conn: Db,
    me: Me,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Bounded page of sources, newest first. Sending neither parameter returns
    the same hundred rows the catalog default always returned, in the same
    order, so an existing client is unaffected."""
    with mapped_errors():
        return content.list_sources(conn, me, library_id, limit, offset)


@router.get("/sources/{source_id}")
def source(
    source_id: UUID,
    conn: Db,
    me: Me,
    from_fragment: int = 0,
    limit: int = 20,
    fragment_id: UUID | None = None,
):
    with mapped_errors():
        return content.read_source(
            conn,
            me,
            source_id,
            from_fragment,
            limit,
            fragment_id=fragment_id,
        )


@router.patch("/sources/{source_id}/metadata")
def metadata(source_id: UUID, body: MetadataInput, conn: Db, me: Me):
    with mapped_errors():
        return writes.update_metadata(conn, me, source_id, body.model_dump(exclude_unset=True))


@router.post("/sources/{source_id}/process", status_code=202)
def process(source_id: UUID, body: ProcessInput, conn: Db, me: Me):
    with mapped_errors():
        return submit_processing(conn, me, source_id, body.idempotency_key, extract=body.extract)


@router.get("/jobs/{job_id}")
def job(job_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return writes.job_status(conn, me, job_id)


@router.get("/jobs")
def jobs(conn: Db, me: Me, library_id: UUID | None = None):
    with mapped_errors():
        return workspace.list_jobs(conn, me, library_id)


@router.get("/experience-summary")
def experience_summary(conn: Db, me: Me):
    with mapped_errors():
        return workspace.experience_summary(conn, me)


@router.get("/retrievals")
def retrievals(conn: Db, me: Me):
    with mapped_errors():
        return workspace.list_retrievals(conn, me)


@router.get("/libraries/{library_id}/experience")
def list_shared_experience(library_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return shared_experience.list_shared(conn, me, library_id)


@router.post("/libraries/{library_id}/experience", status_code=201)
def publish_experience(library_id: UUID, body: PublishExperienceInput, conn: Db, me: Me):
    with mapped_errors():
        return shared_experience.publish(conn, me, library_id, body.use_id, body.outcome_id)


@router.patch("/knowledge/{knowledge_id}")
def edit_knowledge(knowledge_id: UUID, body: workspace.EditKnowledge, conn: Db, me: Me):
    with mapped_errors():
        return workspace.edit_knowledge(conn, me, knowledge_id, body)


@router.post("/libraries/{library_id}/review")
def review_batch(library_id: UUID, body: workspace.ReviewBatch, conn: Db, me: Me):
    with mapped_errors():
        return workspace.review_batch(conn, me, library_id, body)


@router.get("/search")
def search(
    request: Request,
    me: Me,
    q: str = Query(min_length=1, max_length=2000),
    library_id: UUID | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    source_id: list[UUID] | None = Query(default=None, max_length=100),  # noqa: B008
):
    """Search over every library the caller may read, HTTP and MCP alike.

    This route deliberately does not take the ``Db`` lease. A semantic search
    calls the vector provider, and holding a pooled connection for the length
    of that call means the connection is idle for the whole network wait --
    with five in the pool, a handful of concurrent searches starves every
    other route. The shared connection factory is passed instead, and the
    search runs its own short SQL phases around the external call.
    """
    with mapped_errors():
        return content.search_knowledge(
            None,
            me,
            q,
            [library_id] if library_id else None,
            limit,
            semantic=getattr(request.app.state, "semantic_search", None),
            source_ids=source_id,
            connections=getattr(request.app.state, "db_connection", None),
        )


@router.get("/libraries/{library_id}/knowledge")
def knowledge(
    library_id: UUID,
    conn: Db,
    me: Me,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Bounded page of knowledge items, oldest first. The previous behaviour was
    a silent LIMIT 500 with no way to ask for the rest; the default is still
    500, so a caller that sends nothing sees exactly what it saw before."""
    with mapped_errors():
        return writes.list_knowledge(conn, me, library_id, limit, offset)


@router.get("/libraries/{library_id}/rules")
def rules(
    library_id: UUID,
    conn: Db,
    me: Me,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Bounded page of rule versions, drafts beside published history."""
    with mapped_errors():
        return content.list_rules(conn, me, library_id, limit, offset)


@router.get("/rules/{rule_id}")
def rule(rule_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return content.get_rule(conn, me, rule_id)


@router.post("/libraries/{library_id}/rules", status_code=201)
def create_rule(
    library_id: UUID, body: writes.RuleInput, conn: Db, me: Me, replaces: UUID | None = None
):
    with mapped_errors():
        return writes.create_rule(conn, me, library_id, body, replaces)


@router.post("/rules/{rule_id}/publish")
def publish_rule(rule_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return writes.publish(conn, me, rule_id)


@router.post("/knowledge/{knowledge_id}/publish")
def publish_knowledge(knowledge_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return writes.publish(conn, me, knowledge_id, knowledge=True)


@router.post("/rules/{rule_id}/reject")
def reject_rule(rule_id: UUID, conn: Db, me: Me):
    """Withdraw a rule that is not published yet. No body, and no reason field:
    there is nowhere to store one, and CSRF protection is the same as for every
    other state-changing route because the handler adds nothing to it.
    """
    with mapped_errors():
        return writes.reject(conn, me, rule_id)


@router.post("/knowledge/{knowledge_id}/reject")
def reject_knowledge(knowledge_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return writes.reject(conn, me, knowledge_id, knowledge=True)


@router.post("/uses", status_code=201)
def start_use(body: UseInput, conn: Db, me: Me):
    with mapped_errors():
        return writes.start_use(conn, me, **body.model_dump())


@router.get("/uses")
def uses(conn: Db, me: Me):
    with mapped_errors():
        return writes.list_uses(conn, me)


@router.post("/uses/{use_id}/applied")
def applied(use_id: UUID, conn: Db, me: Me):
    with mapped_errors():
        return writes.mark_applied(conn, me, use_id)


@router.post("/uses/{use_id}/outcomes", status_code=201)
def outcome(use_id: UUID, body: OutcomeInput, conn: Db, me: Me):
    with mapped_errors():
        return writes.record_outcome(conn, me, use_id, **body.model_dump())


@router.post("/libraries/{library_id}/export")
def export(library_id: UUID, body: ExportInput, conn: Db, me: Me):
    with mapped_errors():
        return content.export_skill(conn, me, library_id, body.rule_ids)
