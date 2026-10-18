"""C09 — projects: links to libraries and pins to exact rule versions.

The one behaviour that matters here is what :func:`project_context` does with a
link the caller cannot open.

    Membership in a project is not access to the libraries it links.

A project member is entitled to know that their project's mandatory context is
*incomplete* — that is the honest answer, and A12 requires it, because
silently dropping a brand rule would hand them an answer that looks complete
and is not. What they are **not** entitled to is the name, the kind, the id or
the existence of the library behind that gap. So a blocked link is reported as
``library_id=None, library=None`` and a blocked pin as ``rule=None``; the
project member learns *that* their context is blocked, not *what* is blocking
it.

Everything below is read through RLS, so this module never filters by hand to
decide access. It does assemble context, and assembly is where a careless
implementation would re-introduce the leak — for example by joining links to
``kb.library`` to print a title, which would return the title of a library the
caller has no grant on the moment the project view is built.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Literal
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.idempotency import (
    PROJECT_CREATE,
    find_create_operation,
    hash_create_request,
    lock_create_operation,
    record_create_operation,
    validate_idempotency_key,
)
from kb.catalog.libraries import AudienceScope, CreateLibrary
from kb.contracts.entities import Library, Locator, Provenance, Rule
from kb.contracts.enums import LibraryKind, PublicationStatus
from kb.contracts.mcp_tools import GetProjectContextResult

LinkStatus = Literal["available", "blocked"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ============================================================== projects


class CreateProject(_Model):
    organisation_id: UUID
    name: str = Field(min_length=1, max_length=200)
    audience_scope: AudienceScope = "private"
    # NULL stays NULL. A project with no description has none.
    description: str | None = Field(default=None, max_length=4000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ProjectDetail(_Model):
    project: Library
    description: str | None = None
    pinned_at: dt.datetime | None = None
    link_count: int = Field(ge=0)
    required_link_count: int = Field(ge=0)
    pin_count: int = Field(ge=0)


class LinkLibrary(_Model):
    library_id: UUID
    is_required: bool = True


class PinRule(_Model):
    rule_id: UUID
    # The exact version. A pin without a version is not a pin.
    version_no: int = Field(ge=1)
    is_required: bool = True
    priority: int = 0


# ------------------------------------------------- context projections


class LinkedLibrary(_Model):
    """One requirement of the project, as this caller may see it.

    The validator is the mechanism, not decoration: an implementation that
    filled ``library`` for a blocked link raises here rather than shipping a
    name it was not entitled to.
    """

    is_required: bool
    status: LinkStatus
    library_id: UUID | None = None
    library: Library | None = None

    @model_validator(mode="after")
    def _blocked_means_no_description(self) -> LinkedLibrary:
        if self.status == "available":
            if self.library is None or self.library_id is None:
                raise ValueError("an available link must carry the library it resolved to")
            if self.library.id != self.library_id:
                raise ValueError("library_id and library must describe the same library")
        else:
            if self.library is not None or self.library_id is not None:
                raise ValueError(
                    "a blocked link must not carry the id, name or kind of a library "
                    "the caller may not open"
                )
        return self


class PinnedRule(_Model):
    """One pinned version, as this caller may see it."""

    version_no: int = Field(ge=1)
    is_required: bool
    priority: int
    status: LinkStatus
    rule_id: UUID | None = None
    rule: Rule | None = None
    lineage_id: UUID | None = None
    layer: Literal["brand", "project", "general"] | None = None
    content_hash: str | None = None
    bound_at: dt.datetime | None = None

    @model_validator(mode="after")
    def _blocked_means_no_description(self) -> PinnedRule:
        if self.status == "available":
            if self.rule is None or self.rule_id is None:
                raise ValueError("an available pin must carry the rule it resolved to")
        elif any(
            v is not None
            for v in (self.rule, self.rule_id, self.lineage_id, self.layer, self.content_hash)
        ):
            raise ValueError(
                "a blocked pin must not carry the id or text of a rule the caller may not read"
            )
        return self


class ProjectContext(_Model):
    """Everything the project requires, and whether it is all in reach.

    ``complete`` is the field A12 is about. It is False when any *required*
    link or pin cannot be resolved for this caller, which is what stops a
    partially-authorised member being served a "full" project context.
    """

    project: Library
    description: str | None = None
    links: list[LinkedLibrary]
    pins: list[PinnedRule]
    complete: bool
    blocked_link_count: int = Field(ge=0)
    blocked_pin_count: int = Field(ge=0)
    context_state: Literal["complete", "blocked", "conflicted"]
    ordering: list[str] = Field(default_factory=lambda: ["brand", "project", "general"])
    conflicts: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def _state_matches_the_counts(self) -> ProjectContext:
        blocked = self.blocked_link_count + self.blocked_pin_count > 0
        expected_complete = not blocked and not self.conflicts
        if self.complete is not expected_complete:
            raise ValueError("complete must agree with the blocked counts")
        expected_state = "blocked" if blocked else "conflicted" if self.conflicts else "complete"
        if self.context_state != expected_state:
            raise ValueError("context_state must agree with the blocked counts")
        return self

    @property
    def blocked_required_links(self) -> int:
        return sum(1 for link in self.links if link.status == "blocked" and link.is_required)

    def to_mcp_result(self) -> GetProjectContextResult:
        """The shape the MCP ``get_project_context`` tool returns.

        ``unresolved_dependencies`` carries opaque reasons, never library or
        rule names: the MCP surface is remote, and a reason string is still a
        string that ends up in somebody's model context.
        """
        reasons: list[str] = []
        if any(link.status == "blocked" and link.is_required for link in self.links):
            reasons.append("linked_library_access_missing")
        if any(pin.status == "blocked" and pin.is_required for pin in self.pins):
            reasons.append("pinned_rule_access_missing")
        if self.conflicts:
            reasons.append("pinned_version_conflict")
        manifest = self.model_dump(mode="json", exclude_none=True)
        # MCP reports opaque blocked counts, never an inaccessible version.
        manifest["pins"] = [pin for pin in manifest["pins"] if pin["status"] == "available"]
        return GetProjectContextResult(
            project=self.project,
            pinned_rules=[pin.rule for pin in self.pins if pin.rule is not None],
            unresolved_dependencies=reasons,
            manifest=manifest,
            manifest_sha256=hashlib.sha256(
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest(),
        )


# ============================================================== queries


def _as_library(row: tuple) -> Library:
    return Library(
        id=row[0],
        name=row[1],
        kind=LibraryKind(row[2]),
        audience_scope=row[3],
        created_at=row[4],
        generation=row[5],
    )


def _as_rule(row: tuple, actions: list[str]) -> Rule:
    # Rule is the contract's immutable rule version. It has no created_at, and
    # a pinned rule is identified by (id, version_no) — never by "the current
    # one", which is why the pin table carries a foreign key on exactly that
    # pair. The contract refuses to model a *published* rule with no action
    # steps, which is the right refusal: a rule nobody can act on is not a rule
    # that may be served as project context.
    return Rule(
        id=row[0],
        library_id=row[1],
        version_no=row[2],
        title=row[3],
        when_to_apply=row[4],
        expected_effect=row[5],
        verification=row[6],
        publication=PublicationStatus(row[7]),
        actions=actions,
    )


def _assert_fresh_project_replay_allowed(
    cur: psycopg.Cursor, principal: Principal, project_id: UUID
) -> None:
    """Apply the library replay guard to the project's shared library id."""
    if principal.generation_watermark < 1:
        raise AccessDenied("forbidden")
    cur.execute(
        """SELECT 1
           FROM kb.library l
           WHERE l.id = %s
             AND COALESCE(kb.role_rank(kb.effective_role(%s, l.id)), 0) >= 40
             AND kb.membership_allows(%s)""",
        (project_id, principal.principal_id, principal.principal_id),
    )
    if cur.fetchone() is None:
        raise AccessDenied("forbidden")


def create_project(
    conn: psycopg.Connection, principal: Principal, spec: CreateProject
) -> ProjectDetail:
    """Create a project as a library of kind 'project' plus its project row.

    The library, its creator grant, the project row and an optional receipt are
    one transaction. The nested library creation is deliberately keyless: a
    project retry has its own operation type and must never create a second,
    nested library receipt.
    """
    from kb.catalog.libraries import create_library

    key = validate_idempotency_key(spec.idempotency_key)
    request_digest = (
        hash_create_request(
            PROJECT_CREATE,
            spec.model_dump(mode="json", exclude={"idempotency_key"}),
        )
        if key is not None
        else None
    )
    target_id: UUID | None = None

    with transaction_identity(conn, principal):
        if key is not None:
            assert request_digest is not None
            with conn.cursor() as cur:
                lock_create_operation(cur, principal, PROJECT_CREATE, key)
                receipt = find_create_operation(cur, principal, PROJECT_CREATE, key)
                if receipt is not None:
                    if receipt.request_hash != request_digest:
                        raise ValueError("idempotency_conflict")
                    _assert_fresh_project_replay_allowed(cur, principal, receipt.target_id)
                    target_id = receipt.target_id

        if target_id is None:
            library = create_library(
                conn,
                principal,
                CreateLibrary(
                    organisation_id=spec.organisation_id,
                    name=spec.name,
                    kind=LibraryKind.PROJECT,
                    audience_scope=spec.audience_scope,
                    idempotency_key=None,
                ),
            )
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kb.project (id, description) VALUES (%s, %s)",
                    (library.id, spec.description),
                )
                if key is not None:
                    assert request_digest is not None
                    record_create_operation(
                        cur,
                        principal,
                        PROJECT_CREATE,
                        key,
                        request_digest,
                        library.id,
                    )
            target_id = library.id

    assert target_id is not None
    result = get_project(conn, principal, target_id)
    if result is None:
        raise AccessDenied("forbidden")
    return result


def get_project(
    conn: psycopg.Connection, principal: Principal, project_id: UUID
) -> ProjectDetail | None:
    """Project metadata and its manifest sizes. None when not visible."""
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.id, l.name, l.kind, l.audience_scope, l.created_at, l.generation,
                   p.description, p.pinned_at,
                   (SELECT count(*) FROM kb.project_library_link k
                     WHERE k.project_id = p.id),
                   (SELECT count(*) FROM kb.project_library_link k
                     WHERE k.project_id = p.id AND k.is_required),
                   (SELECT count(*) FROM kb.project_rule_pin r
                     WHERE r.project_id = p.id)
            FROM kb.project p
            JOIN kb.library l ON l.id = p.id
            WHERE p.id = %s
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return ProjectDetail(
        project=_as_library(row),
        description=row[6],
        pinned_at=row[7],
        link_count=int(row[8]),
        required_link_count=int(row[9]),
        pin_count=int(row[10]),
    )


def link_library(
    conn: psycopg.Connection, principal: Principal, project_id: UUID, spec: LinkLibrary
) -> None:
    """Record that a project requires a library.

    This writes a requirement. It writes no grant, creates no session and
    touches no role. The manager performing it gains nothing on the target
    library, and neither does anyone who later joins the project.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kb.project_library_link (project_id, library_id, is_required) "
            "VALUES (%s, %s, %s)",
            (project_id, spec.library_id, spec.is_required),
        )


def unlink_library(
    conn: psycopg.Connection, principal: Principal, project_id: UUID, library_id: UUID
) -> None:
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM kb.project_library_link WHERE project_id = %s AND library_id = %s",
            (project_id, library_id),
        )
        if cur.rowcount == 0:
            raise AccessDenied("no such link, or you may not curate this project")


def pin_rule(
    conn: psycopg.Connection, principal: Principal, project_id: UUID, spec: PinRule
) -> None:
    """Pin one exact rule version as a project requirement.

    The database refuses a version that does not exist
    (``pin_names_one_exact_version``) and the policy refuses a rule library the
    caller cannot curate, so "pin the current version of a rule I cannot read"
    is not reachable even in principle.
    """
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb.project_rule_pin
                (project_id, rule_id, version_no, is_required, priority)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (project_id, spec.rule_id, spec.version_no, spec.is_required, spec.priority),
        )
        cur.execute("UPDATE kb.project SET pinned_at = now() WHERE id = %s", (project_id,))


def unpin_rule(
    conn: psycopg.Connection, principal: Principal, project_id: UUID, rule_id: UUID
) -> None:
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM kb.project_rule_pin WHERE project_id = %s AND rule_id = %s",
            (project_id, rule_id),
        )
        if cur.rowcount == 0:
            raise AccessDenied("no such pin, or you may not curate this project")


# ============================================================== context


def _actions_for(
    conn: psycopg.Connection, principal: Principal, rule_ids: list[UUID]
) -> dict[UUID, list[str]]:
    """Action steps per rule, for rules this caller may already read.

    ``kb.rule_action`` is not one of the tables 0002 puts under RLS, so reading
    it is *not* self-protecting. The EXISTS against ``kb.rule`` is therefore the
    only thing standing between this query and a disclosure, and it is there on
    purpose: ``kb.rule`` is under FORCE RLS, so a caller who cannot read a rule
    gets no action text for it either.

    The consequence is written down rather than left to be rediscovered: this
    function is only safe while callers pass rule ids that already resolved
    through ``kb.rule``. Handing it an id from anywhere else would serve the
    step list of a rule the caller has no grant on. Giving
    ``kb.rule_action`` a policy of its own belongs to the owner of 0002, not to
    this card — so the guard lives here and the finding is reported.
    """
    if not rule_ids:
        return {}
    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.rule_id, a.body
            FROM kb.rule_action a
            WHERE a.rule_id = ANY(%s)
              AND EXISTS (SELECT 1 FROM kb.rule r WHERE r.id = a.rule_id)
            ORDER BY a.rule_id, a.ordinal
            """,
            (rule_ids,),
        )
        rows = cur.fetchall()
    collected: dict[UUID, list[str]] = {rule_id: [] for rule_id in rule_ids}
    for rule_id, body in rows:
        collected[rule_id].append(body)
    return collected


def project_context(
    conn: psycopg.Connection, principal: Principal, project_id: UUID
) -> ProjectContext | None:
    """Assemble the project's mandatory context *for this caller*.

    Returns None when the project itself is not visible — the same answer as
    "there is no such project".

    Links and pins are read from their RLS-scoped relations. A link row the
    caller can see (they hold reader on the *project*) names a library they may
    not open; the LEFT JOIN to ``kb.library`` is what RLS empties, and the row
    that comes back is therefore the honest "there is a requirement here and it
    is not available to you" — with no id, no name, no kind.
    """
    project = get_project(conn, principal, project_id)
    if project is None:
        return None

    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT k.is_required, l.id, l.name, l.kind, l.audience_scope,
                   l.created_at, l.generation
            FROM kb.project_library_link k
            LEFT JOIN kb.library l ON l.id = k.library_id
            WHERE k.project_id = %s
            ORDER BY k.is_required DESC, l.name NULLS FIRST, k.created_at
            """,
            (project_id,),
        )
        link_rows = cur.fetchall()

    links: list[LinkedLibrary] = []
    for row in link_rows:
        resolved = row[1] is not None
        links.append(
            LinkedLibrary(
                is_required=row[0],
                status="available" if resolved else "blocked",
                library_id=row[1] if resolved else None,
                library=_as_library(row[1:7]) if resolved else None,
            )
        )

    with transaction_identity(conn, principal), conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.version_no, r.is_required, r.priority,
                   ru.id, ru.library_id, ru.version_no, ru.title,
                   ru.when_to_apply, ru.expected_effect, ru.verification,
                   ru.publication, ru.lineage_id, l.kind, r.created_at
            FROM kb.project_rule_pin r
            LEFT JOIN kb.rule ru ON ru.id = r.rule_id AND ru.version_no = r.version_no
            LEFT JOIN kb.library l ON l.id=ru.library_id
            WHERE r.project_id = %s
            ORDER BY r.priority DESC, r.is_required DESC, r.created_at
            """,
            (project_id,),
        )
        pin_rows = cur.fetchall()

    from kb.catalog.content import get_rule

    pins: list[PinnedRule] = []
    for row in pin_rows:
        resolved = row[3] is not None
        full_rule = None
        rule_hash = None
        if resolved:
            try:
                raw = get_rule(conn, principal, row[3], row[5], _record_delivery=False)
            except AccessDenied:
                # A grant may have been revoked since the pin query.
                resolved = False
            else:
                evidence = []
                for item in raw["provenance"]:
                    located = item.get("locator")
                    locator = None
                    if located:
                        values = {k: v for k, v in located.items() if k in Locator.model_fields}
                        values["kind"] = located["locator_kind"]
                        if located.get("table_index") is not None:
                            values["table"] = located["table_index"]
                        if (
                            located.get("cell_row") is not None
                            and located.get("cell_column") is not None
                        ):
                            values["cell"] = (located["cell_row"], located["cell_column"])
                        locator = Locator(**values)
                    evidence.append(
                        Provenance(
                            source_id=item["source_id"],
                            fragment_id=item["fragment_id"],
                            locator=locator,
                            content_hash=item["content_hash"],
                            author=item["author"],
                            publication=item["publication_ref"],
                            url=item["url"],
                            retrieved_at=item["retrieved_at"],
                        )
                    )
                full_rule = Rule(
                    **{
                        k: v for k, v in raw.items() if k in Rule.model_fields and k != "provenance"
                    },
                    provenance=evidence,
                )
                rule_hash = hashlib.sha256(
                    json.dumps(
                        full_rule.model_dump(mode="json"),
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
        pins.append(
            PinnedRule(
                version_no=row[0],
                is_required=row[1],
                priority=row[2],
                status="available" if resolved else "blocked",
                rule_id=row[3] if resolved else None,
                rule=full_rule,
                lineage_id=row[11] if resolved else None,
                layer=(
                    "brand"
                    if row[12] == "brand"
                    else "project"
                    if row[12] == "project"
                    else "general"
                )
                if resolved
                else None,
                content_hash=rule_hash,
                bound_at=row[13],
            )
        )

    layers = {"brand": 0, "project": 1, "general": 2, None: 3}
    pins.sort(
        key=lambda pin: (
            layers[pin.layer],
            -pin.priority,
            not pin.is_required,
            str(pin.rule_id or ""),
        )
    )
    lineages: dict[UUID, list[PinnedRule]] = {}
    for pin in pins:
        if pin.lineage_id is not None:
            lineages.setdefault(pin.lineage_id, []).append(pin)
    conflicts = [
        {
            "code": "multiple_versions_pinned",
            "lineage_id": str(lineage),
            "rule_ids": [str(pin.rule_id) for pin in versions],
        }
        for lineage, versions in lineages.items()
        if len({pin.version_no for pin in versions}) > 1
    ]

    blocked_links = sum(1 for link in links if link.status == "blocked")
    blocked_pins = sum(1 for pin in pins if pin.status == "blocked")
    # Any unresolvable requirement makes the context incomplete, required or
    # not. An optional link the caller cannot open still means part of this
    # project is unavailable to them, and reporting a "complete" context while
    # quietly holding back a library is the failure mode A12 names.
    complete = blocked_links + blocked_pins == 0 and not conflicts
    result = ProjectContext(
        project=project.project,
        description=project.description,
        links=links,
        pins=pins,
        complete=complete,
        blocked_link_count=blocked_links,
        blocked_pin_count=blocked_pins,
        context_state="blocked"
        if blocked_links + blocked_pins
        else "conflicted"
        if conflicts
        else "complete",
        conflicts=conflicts,
    )
    from kb.catalog.retrieval_events import record_retrieval

    with transaction_identity(conn, principal), conn.cursor() as cur:
        for pin in pins:
            if pin.status == "available":
                record_retrieval(
                    cur,
                    principal,
                    item_kind="rule",
                    item_id=pin.rule_id,
                    version_no=pin.version_no,
                )
    return result
