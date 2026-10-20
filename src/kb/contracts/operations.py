"""The shared operation registry: one declaration per product operation.

Before this module the HTTP router, the MCP surface and the bearer scope table
each carried their own private copy of "which operation is this, which method,
which path, which scope, does it change state". Three copies drift. The visible
consequence of that drift is a route that answers under one scope in the
browser and under another over MCP, and an alias prefix that quietly loses an
admin check because the scope table matched on a literal ``/api`` string.

So the registry is the single declaration, and everything else either consumes
it or is asserted against it:

* :mod:`kb.http.router` mounts the transport under both prefixes and verifies
  that every registered operation resolves to a real route.
* :mod:`kb.mcp.server` verifies that every registered MCP tool keeps the scope
  its handler has always required.
* :mod:`kb.access.bearer_policy` normalises a request path through
  :func:`canonical_api_path` so ``/api`` and ``/api/v1`` cannot diverge.

This module is deliberately free of ``kb`` imports. It is contract data, and it
must be importable from the contracts package without pulling in FastAPI, the
database layer or the identity stack.

The HTTP path templates here are prefix-free: the same declaration serves the
canonical ``/api/v1`` prefix and the compatible ``/api`` alias. The MCP tool
column is the *existing* tool name from :mod:`kb.contracts.mcp_tools`; nothing
in this module renames a tool, a route, a scope or a behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

#: Contract revision for the operation declarations below. Bump the patch part
#: for a wording or docstring edit, the minor part when a declaration is added,
#: and never silently change an existing (method, path, scope) triple.
CONTRACT_REVISION: Final[str] = "architecture-20261015-v3"

#: The canonical prefix. New clients use it; the schema is served from it.
API_VERSION: Final[str] = "v1"
CANONICAL_PREFIX: Final[str] = f"/api/{API_VERSION}"

#: The prefix that already shipped. It stays byte-compatible and is kept out of
#: the OpenAPI schema so the two prefixes cannot produce duplicate operation ids.
ALIAS_PREFIX: Final[str] = "/api"

#: Canonical first, alias second. Every consumer keeps this order.
API_PREFIXES: Final[tuple[str, ...]] = (CANONICAL_PREFIX, ALIAS_PREFIX)


class Effect(StrEnum):
    """Whether fulfilling an operation changes persisted state.

    ``read`` operations are safe to retry and must never appear with a method
    that mutates. ``write`` operations are the ones that need an idempotency
    key, a CSRF check and exactly one dispatch — the reason the alias mount is
    asserted to add paths rather than to add a second call.
    """

    READ = "read"
    WRITE = "write"


READ_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True, slots=True)
class Page:
    """Server-side pagination declared for one list operation.

    ``limit_default`` is the value a caller gets when it sends no parameters.
    It is not a new default invented for v1: it is the limit the catalog
    function already applied, so an existing client that sends nothing sees
    exactly the rows it saw before.
    """

    limit_default: int
    limit_max: int
    offset_default: int | None = 0

    def __post_init__(self) -> None:
        if not 1 <= self.limit_default <= self.limit_max:
            raise ValueError("limit_default must be within 1..limit_max")
        if self.offset_default is not None and self.offset_default < 0:
            raise ValueError("offset_default must not be negative")


@dataclass(frozen=True, slots=True)
class Operation:
    """One product operation, declared once for every surface that serves it.

    ``path`` is the prefix-free template exactly as the router declares it, so
    a drift between this table and the router is a plain string mismatch that
    the registry test can report, not a semantic guess.
    """

    name: str
    method: str
    path: str
    effect: Effect
    scope: str
    summary: str
    mcp_tool: str | None = None
    page: Page | None = None

    @property
    def route_key(self) -> tuple[str, str]:
        """Identity of the route itself, independent of which prefix serves it."""
        return (self.method, self.path)

    @property
    def canonical_path(self) -> str:
        return f"{CANONICAL_PREFIX}{self.path}"

    @property
    def alias_path(self) -> str:
        return f"{ALIAS_PREFIX}{self.path}"

    def full_path(self, prefix: str) -> str:
        return f"{prefix}{self.path}"

    @property
    def is_paginated(self) -> bool:
        return self.page is not None


def _op(
    name: str,
    method: str,
    path: str,
    effect: Effect,
    scope: str,
    summary: str,
    mcp_tool: str | None = None,
    page: Page | None = None,
) -> Operation:
    return Operation(
        name=name,
        method=method,
        path=path,
        effect=effect,
        scope=scope,
        summary=summary,
        mcp_tool=mcp_tool,
        page=page,
    )


_READ = Effect.READ
_WRITE = Effect.WRITE

#: The whole registry. Order is documentation order, not dispatch order.
OPERATIONS: Final[tuple[Operation, ...]] = (
    _op(
        "auth.browser.approve",
        "POST",
        "/browser-logins/{pairing_id}/approve",
        _WRITE,
        "kb:read",
        "Approve one bound browser login as the same verified user.",
        mcp_tool="approve_browser_login",
    ),
    # ------------------------------------------------------------ libraries
    _op(
        "libraries.list",
        "GET",
        "/libraries",
        _READ,
        "kb:read",
        "List libraries the caller can see.",
        mcp_tool="list_libraries",
    ),
    _op(
        "libraries.create",
        "POST",
        "/libraries",
        _WRITE,
        "kb:contribute",
        "Create a library.",
    ),
    _op(
        "libraries.read",
        "GET",
        "/libraries/{library_id}",
        _READ,
        "kb:read",
        "Read one library.",
    ),
    # Bounded server-side pagination. The defaults are the catalog defaults.
    _op(
        "libraries.sources",
        "GET",
        "/libraries/{library_id}/sources",
        _READ,
        "kb:read",
        "List RLS-visible sources in a library, newest first.",
        page=Page(limit_default=100, limit_max=100),
    ),
    _op(
        "libraries.knowledge",
        "GET",
        "/libraries/{library_id}/knowledge",
        _READ,
        "kb:read",
        "List knowledge items in a library, oldest first.",
        page=Page(limit_default=500, limit_max=500),
    ),
    _op(
        "libraries.rules",
        "GET",
        "/libraries/{library_id}/rules",
        _READ,
        "kb:read",
        "List rule versions in a library, published history beside drafts.",
        page=Page(limit_default=100, limit_max=100),
    ),
    _op(
        "libraries.policy.read",
        "GET",
        "/libraries/{library_id}/policy",
        _READ,
        "kb:read",
        "Read the library policy.",
        mcp_tool="read_library_policy",
    ),
    _op(
        "libraries.policy.write",
        "PUT",
        "/libraries/{library_id}/policy",
        _WRITE,
        "kb:admin",
        "Replace the library policy.",
    ),
    _op(
        "libraries.experience.list",
        "GET",
        "/libraries/{library_id}/experience",
        _READ,
        "kb:read",
        "List published shared experience for a library.",
        mcp_tool="list_shared_experience",
    ),
    _op(
        "libraries.experience.publish",
        "POST",
        "/libraries/{library_id}/experience",
        _WRITE,
        "kb:experience",
        "Publish a shared experience from a use and its outcome.",
        mcp_tool="publish_experience",
    ),
    _op(
        "libraries.review",
        "POST",
        "/libraries/{library_id}/review",
        _WRITE,
        "kb:contribute",
        "Publish or reject a batch of reviewed items.",
        mcp_tool="review_items",
    ),
    _op(
        "libraries.export",
        "POST",
        "/libraries/{library_id}/export",
        _WRITE,
        "kb:export",
        "Export the selected rules as a skill file.",
        mcp_tool="export_skill",
    ),
    # --------------------------------------------------------------- sources
    _op(
        "sources.submit.url",
        "POST",
        "/libraries/{library_id}/sources/from-url",
        _WRITE,
        "kb:contribute",
        "Submit a source by link for durable ingestion.",
        mcp_tool="submit_source",
    ),
    _op(
        "sources.submit.upload",
        "POST",
        "/libraries/{library_id}/sources/upload",
        _WRITE,
        "kb:contribute",
        "Submit an uploaded original file for durable ingestion.",
    ),
    _op(
        "sources.read",
        "GET",
        "/sources/{source_id}",
        _READ,
        "kb:read",
        "Read one source and a bounded, ordinal-ordered fragment page.",
        mcp_tool="read_source",
    ),
    _op(
        "sources.original",
        "GET",
        "/sources/{source_id}/original",
        _READ,
        "kb:export",
        "Stream the original file bytes.",
    ),
    _op(
        "sources.metadata",
        "PATCH",
        "/sources/{source_id}/metadata",
        _WRITE,
        "kb:contribute",
        "Update source citation metadata.",
    ),
    _op(
        "sources.process",
        "POST",
        "/sources/{source_id}/process",
        _WRITE,
        "kb:contribute",
        "Queue a source for processing.",
    ),
    # ------------------------------------------------------------- knowledge
    _op(
        "knowledge.edit",
        "PATCH",
        "/knowledge/{knowledge_id}",
        _WRITE,
        "kb:contribute",
        "Edit a knowledge item against its expected statement.",
        mcp_tool="edit_knowledge",
    ),
    _op(
        "knowledge.publish",
        "POST",
        "/knowledge/{knowledge_id}/publish",
        _WRITE,
        "kb:contribute",
        "Publish a knowledge item.",
        mcp_tool="publish_knowledge",
    ),
    _op(
        "knowledge.reject",
        "POST",
        "/knowledge/{knowledge_id}/reject",
        _WRITE,
        "kb:contribute",
        "Withdraw an unpublished knowledge item.",
    ),
    # ----------------------------------------------------------------- rules
    _op(
        "rules.read",
        "GET",
        "/rules/{rule_id}",
        _READ,
        "kb:read",
        "Read one exact rule version.",
        mcp_tool="get_rule",
    ),
    _op(
        "rules.create",
        "POST",
        "/libraries/{library_id}/rules",
        _WRITE,
        "kb:contribute",
        "Create a rule version.",
    ),
    _op(
        "rules.publish",
        "POST",
        "/rules/{rule_id}/publish",
        _WRITE,
        "kb:contribute",
        "Publish a rule version.",
    ),
    _op(
        "rules.reject",
        "POST",
        "/rules/{rule_id}/reject",
        _WRITE,
        "kb:contribute",
        "Withdraw an unpublished rule version.",
    ),
    # ------------------------------------------------------- search and jobs
    _op(
        "knowledge.search",
        "GET",
        "/search",
        _READ,
        "kb:read",
        "Search knowledge, lexically and optionally semantically.",
        mcp_tool="search_knowledge",
        page=Page(limit_default=10, limit_max=50, offset_default=None),
    ),
    _op(
        "jobs.read",
        "GET",
        "/jobs/{job_id}",
        _READ,
        "kb:read",
        "Read one ingestion job.",
        mcp_tool="job_status",
    ),
    _op(
        "jobs.list",
        "GET",
        "/jobs",
        _READ,
        "kb:read",
        "List ingestion jobs.",
        mcp_tool="list_jobs",
    ),
    _op(
        "experience.summary",
        "GET",
        "/experience-summary",
        _READ,
        "kb:read",
        "Summarise retrieval, use and outcome experience.",
        mcp_tool="get_experience_summary",
    ),
    _op(
        "retrievals.list",
        "GET",
        "/retrievals",
        _READ,
        "kb:read",
        "List recorded retrieval events.",
        mcp_tool="list_retrievals",
    ),
    # ------------------------------------------------------------ experience
    _op(
        "uses.start",
        "POST",
        "/uses",
        _WRITE,
        "kb:experience",
        "Record the start of a rule use.",
        mcp_tool="start_use",
    ),
    _op(
        "uses.list",
        "GET",
        "/uses",
        _READ,
        "kb:read",
        "List recorded rule uses.",
    ),
    _op(
        "uses.applied",
        "POST",
        "/uses/{use_id}/applied",
        _WRITE,
        "kb:experience",
        "Mark a rule use as applied.",
    ),
    _op(
        "uses.outcomes",
        "POST",
        "/uses/{use_id}/outcomes",
        _WRITE,
        "kb:experience",
        "Record the outcome of a rule use.",
        mcp_tool="record_outcome",
    ),
    # -------------------------------------------------------------- projects
    _op(
        "projects.context",
        "GET",
        "/projects/{project_id}/context",
        _READ,
        "kb:read",
        "Read validated project context and its pinned rules.",
        mcp_tool="get_project_context",
    ),
    # -------------------------------------------------------------- research
    _op(
        "research.sources",
        "GET",
        "/research/sources",
        _READ,
        "kb:read",
        "List sources available to a research run.",
        mcp_tool="research_sources",
    ),
    _op(
        "research.list",
        "GET",
        "/research",
        _READ,
        "kb:read",
        "List research runs for a library.",
        mcp_tool="list_research",
    ),
    _op(
        "research.start",
        "POST",
        "/research",
        _WRITE,
        "kb:contribute",
        "Start a research run.",
        mcp_tool="start_research",
    ),
    _op(
        "research.status",
        "GET",
        "/research/{run_id}",
        _READ,
        "kb:read",
        "Read one research run.",
        mcp_tool="research_status",
    ),
    _op(
        "research.export",
        "GET",
        "/research/{run_id}/markdown",
        _READ,
        "kb:export",
        "Export a research run as markdown.",
        mcp_tool="export_research",
    ),
    _op(
        "research.draft_rule",
        "POST",
        "/research/{run_id}/draft-rule",
        _WRITE,
        "kb:contribute",
        "Draft a rule from a research run.",
        mcp_tool="draft_rule_from_research",
    ),
    # ----------------------------------------------------------------- notes
    _op(
        "notes.list",
        "GET",
        "/notes",
        _READ,
        "kb:read",
        "List research notes in a library.",
        mcp_tool="list_research_notes",
    ),
    _op(
        "notes.create",
        "POST",
        "/notes",
        _WRITE,
        "kb:contribute",
        "Save a note against a research run.",
        mcp_tool="save_research_note",
    ),
    _op(
        "notes.read",
        "GET",
        "/notes/{note_id}",
        _READ,
        "kb:read",
        "Read one research note.",
        mcp_tool="read_research_note",
    ),
    _op(
        "notes.update",
        "PATCH",
        "/notes/{note_id}",
        _WRITE,
        "kb:contribute",
        "Update a research note.",
    ),
    # ---------------------------------------------------------------- graphs
    _op(
        "graphs.provenance",
        "GET",
        "/graphs/provenance",
        _READ,
        "kb:read",
        "Build a bounded provenance graph.",
        mcp_tool="get_knowledge_graph",
    ),
    _op(
        "graphs.semantic",
        "GET",
        "/graphs/semantic",
        _READ,
        "kb:read",
        "Build the semantic network for a library.",
        mcp_tool="get_semantic_network",
    ),
    _op(
        "graphs.concept.read",
        "GET",
        "/graphs/concepts/{concept_id}",
        _READ,
        "kb:read",
        "Read one semantic concept with freshly validated evidence.",
    ),
    _op(
        "graphs.similarity.refresh",
        "POST",
        "/graphs/similarity",
        _WRITE,
        "kb:contribute",
        "Queue a refresh of the derived concept similarity layer.",
        mcp_tool="refresh_concept_similarity",
    ),
    _op(
        "graphs.concepts",
        "POST",
        "/graphs/concepts",
        _WRITE,
        "kb:contribute",
        "Create a semantic concept.",
        mcp_tool="create_semantic_concept",
    ),
    _op(
        "graphs.relations",
        "POST",
        "/graphs/relations",
        _WRITE,
        "kb:contribute",
        "Create a semantic relation.",
        mcp_tool="create_semantic_relation",
    ),
    _op(
        "graphs.review",
        "POST",
        "/graphs/review",
        _WRITE,
        "kb:contribute",
        "Review a semantic relation.",
        mcp_tool="review_semantic_relation",
    ),
    # ------------------------------------- served but previously undeclared
    # These 33 routes are served by the same application under the same
    # prefixes. They were not declared, which meant a registry-first scope
    # lookup would have closed every one of them. Each scope below is the
    # value the frozen pattern table already returned, so declaring them
    # changes where the answer comes from and not what it is.
    _op(
        "libraries.list_grants",
        "GET",
        "/libraries/{library_id}/grants",
        _READ,
        "kb:read",
        "List the principals a library has been granted to.",
    ),
    _op(
        "libraries.set_grant",
        "PUT",
        "/libraries/{library_id}/grants",
        _WRITE,
        "kb:admin",
        "Replace the grant set of one library.",
    ),
    _op(
        "libraries.revoke_grant",
        "DELETE",
        "/libraries/{library_id}/grants/{principal_id}",
        _WRITE,
        "kb:admin",
        "Revoke one principal's grant on a library.",
    ),
    _op(
        "library_types.list",
        "GET",
        "/library-types",
        _READ,
        "kb:read",
        "List the library kinds this deployment offers.",
    ),
    _op(
        "library_types.create",
        "POST",
        "/library-types",
        _WRITE,
        "kb:contribute",
        "Create a library kind.",
    ),
    _op(
        "session.read_me",
        "GET",
        "/me",
        _READ,
        "kb:read",
        "Read the caller's own principal and memberships.",
    ),
    _op(
        "projects.create",
        "POST",
        "/projects",
        _WRITE,
        "kb:contribute",
        "Create a project.",
    ),
    _op(
        "projects.read",
        "GET",
        "/projects/{project_id}",
        _READ,
        "kb:read",
        "Read one project.",
    ),
    _op(
        "projects.attach_library",
        "POST",
        "/projects/{project_id}/libraries",
        _WRITE,
        "kb:contribute",
        "Attach a library to a project.",
    ),
    _op(
        "projects.detach_library",
        "DELETE",
        "/projects/{project_id}/libraries/{library_id}",
        _WRITE,
        "kb:contribute",
        "Detach a library from a project.",
    ),
    _op(
        "projects.create_rule",
        "POST",
        "/projects/{project_id}/rules",
        _WRITE,
        "kb:contribute",
        "Create a rule inside a project.",
    ),
    _op(
        "projects.delete_rule",
        "DELETE",
        "/projects/{project_id}/rules/{rule_id}",
        _WRITE,
        "kb:contribute",
        "Delete one rule from a project.",
    ),
    _op(
        "admin.groups.list",
        "GET",
        "/orgs/{organisation_id}/groups",
        _READ,
        "kb:admin",
        "List an organisation's groups.",
    ),
    _op(
        "admin.groups.create",
        "POST",
        "/orgs/{organisation_id}/groups",
        _WRITE,
        "kb:admin",
        "Create a group.",
    ),
    _op(
        "admin.groups.list_group_grants",
        "GET",
        "/orgs/{organisation_id}/groups/{group_id}/grants",
        _READ,
        "kb:admin",
        "List the libraries one group has been granted.",
    ),
    _op(
        "admin.groups.set_group_library_grant",
        "PUT",
        "/orgs/{organisation_id}/groups/{group_id}/grants/{library_id}",
        _WRITE,
        "kb:admin",
        "Grant one group access to one library.",
    ),
    _op(
        "admin.groups.revoke_library_grant",
        "DELETE",
        "/orgs/{organisation_id}/groups/{group_id}/grants/{library_id}",
        _WRITE,
        "kb:admin",
        "Revoke one group's access to one library.",
    ),
    _op(
        "admin.groups.set_member_role",
        "PUT",
        "/orgs/{organisation_id}/groups/{group_id}/members/{principal_id}",
        _WRITE,
        "kb:admin",
        "Set one principal's role in one group.",
    ),
    _op(
        "admin.groups.remove_member",
        "DELETE",
        "/orgs/{organisation_id}/groups/{group_id}/members/{principal_id}",
        _WRITE,
        "kb:admin",
        "Remove one principal from one group.",
    ),
    _op(
        "admin.members.list",
        "GET",
        "/orgs/{organisation_id}/members",
        _READ,
        "kb:admin",
        "List an organisation's members.",
    ),
    _op(
        "admin.members.read",
        "GET",
        "/orgs/{organisation_id}/members/{principal_id}",
        _READ,
        "kb:admin",
        "Read one member.",
    ),
    _op(
        "admin.members.accept_invitation",
        "POST",
        "/orgs/{organisation_id}/members/me",
        _WRITE,
        "kb:admin",
        "Accept the caller's own invitation into the organisation.",
    ),
    _op(
        "admin.members.deactivate",
        "POST",
        "/orgs/{organisation_id}/members/{principal_id}/deactivate",
        _WRITE,
        "kb:admin",
        "Deactivate one member and release their grants.",
    ),
    _op(
        "admin.invitations.list",
        "GET",
        "/orgs/{organisation_id}/invitations",
        _READ,
        "kb:admin",
        "List outstanding invitations.",
    ),
    _op(
        "admin.invitations.create",
        "POST",
        "/orgs/{organisation_id}/invitations",
        _WRITE,
        "kb:admin",
        "Invite a principal into the organisation.",
    ),
    _op(
        "admin.invitations.accept",
        "POST",
        "/orgs/{organisation_id}/invitations/{invitation_id}/accept",
        _WRITE,
        "kb:admin",
        "Accept one invitation as its addressee.",
    ),
    _op(
        "admin.invitations.notify",
        "POST",
        "/orgs/{organisation_id}/invitations/{invitation_id}/notify",
        _WRITE,
        "kb:admin",
        "Re-send one outstanding invitation.",
    ),
    _op(
        "admin.invitations.withdraw",
        "POST",
        "/orgs/{organisation_id}/invitations/{invitation_id}/withdraw",
        _WRITE,
        "kb:admin",
        "Withdraw one outstanding invitation.",
    ),
    _op(
        "admin.revocations.list",
        "GET",
        "/orgs/{organisation_id}/outstanding-revocations",
        _READ,
        "kb:admin",
        "List revocations that have not finished propagating.",
    ),
    _op(
        "admin.revocations.retry",
        "POST",
        "/orgs/{organisation_id}/outstanding-revocations/retry",
        _WRITE,
        "kb:admin",
        "Retry the outstanding revocations.",
    ),
    _op(
        "admin.policy_journal.read",
        "GET",
        "/orgs/{organisation_id}/policy-journal",
        _READ,
        "kb:admin",
        "Read the organisation's policy change journal.",
    ),
    _op(
        "admin.orgs.read_self",
        "GET",
        "/orgs/{organisation_id}/me",
        _READ,
        "kb:admin",
        "Read the caller's own administrative view of the organisation.",
    ),
    _op(
        "admin.access.read_library_access",
        "GET",
        "/orgs/{organisation_id}/access/{principal_id}/libraries/{library_id}",
        _READ,
        "kb:admin",
        "Read why one principal has access to one library.",
    ),
)

OPERATIONS_BY_NAME: Final[dict[str, Operation]] = {op.name: op for op in OPERATIONS}

#: MCP tool name -> operation. The tool names are the existing contract names.
MCP_OPERATIONS: Final[dict[str, Operation]] = {
    op.mcp_tool: op for op in OPERATIONS if op.mcp_tool is not None
}

#: The list operations that paginate on the server, keyed by canonical path.
PAGINATED_OPERATIONS: Final[dict[str, Operation]] = {
    op.path: op for op in OPERATIONS if op.is_paginated
}

#: Families whose list routes must be bounded rather than silently truncated.
PAGINATED_FAMILIES: Final[tuple[str, ...]] = ("sources", "knowledge", "rules")


def canonical_api_path(path: str) -> str | None:
    """Return ``path`` under the canonical prefix, or ``None`` if it is not one.

    Both shipped prefixes collapse onto the same canonical path before any scope
    rule looks at it. That is what stops ``/api/v1`` from answering a grants
    route that ``/api`` would have held to ``kb:admin``.

    A request for a bare prefix is returned as the canonical prefix rather than
    passed through, because the docstring promises a canonical value and a
    caller that then slices off :data:`CANONICAL_PREFIX` must not be handed a
    shorter string than the prefix it is slicing.
    """
    for prefix in sorted(API_PREFIXES, key=len, reverse=True):
        if path == prefix:
            return CANONICAL_PREFIX
        if path.startswith(f"{prefix}/"):
            return f"{CANONICAL_PREFIX}{path[len(prefix) :]}"
    return None


def _segments(path: str) -> tuple[str, ...]:
    return tuple(segment for segment in path.split("/") if segment)


def _is_parameter(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _literal_count(path: str) -> int:
    return sum(1 for segment in _segments(path) if not _is_parameter(segment))


def _template_matches(path: str, segments: tuple[str, ...]) -> bool:
    """Does a concrete segment list satisfy this route template?

    Segment counts must be equal, a literal must equal the segment, and a
    ``{parameter}`` matches exactly one non-empty segment. A parameter does not
    span a slash, so ``/libraries/{library_id}`` cannot swallow
    ``/libraries/a/b``.
    """
    declared = _segments(path)
    if len(declared) != len(segments):
        return False
    for template_segment, actual in zip(declared, segments, strict=True):
        if _is_parameter(template_segment):
            if not actual:
                return False
        elif template_segment != actual:
            return False
    return True


#: (method, segment count) -> operations with that shape, most literal first.
#: Ordering by descending literal count is what makes a static route win over a
#: parameterised one, which is the order the router resolves them in.
_TEMPLATE_INDEX: Final[dict[tuple[str, int], tuple[Operation, ...]]] = {}


def _build_template_index() -> dict[tuple[str, int], tuple[Operation, ...]]:
    grouped: dict[tuple[str, int], list[Operation]] = {}
    for op in OPERATIONS:
        grouped.setdefault((op.method, len(_segments(op.path))), []).append(op)
    return {
        key: tuple(sorted(ops, key=lambda op: (-_literal_count(op.path), op.path)))
        for key, ops in grouped.items()
    }


_TEMPLATE_INDEX.update(_build_template_index())


def operation_for_template(method: str, path: str) -> Operation | None:
    """Resolve a concrete, prefix-free path against the registered templates.

    Returns ``None`` when no template matches, and also when two templates
    match equally well. Ambiguity is closed rather than guessed: if a path is
    not owned by exactly one operation it gets no scope, which is the contract's
    rule for an unknown protected operation and the only safe reading of a
    path two operations both claim.

    A trailing slash is refused for the same reason. The router redirects
    ``/libraries/`` to ``/libraries`` rather than serving it, so a path that
    shape is not one any operation is actually serving.
    """
    method = method.upper()
    if path.endswith("/") and path != "/":
        return None

    segments = _segments(path)
    candidates = _TEMPLATE_INDEX.get((method, len(segments)), ())
    if not candidates:
        return None

    best: Operation | None = None
    best_literals = -1
    ties = 0
    for op in candidates:
        if not _template_matches(op.path, segments):
            continue
        literals = _literal_count(op.path)
        if literals > best_literals:
            best, best_literals, ties = op, literals, 1
        elif literals == best_literals:
            ties += 1
    if best is None or ties > 1:
        return None
    return best


def operation_for(method: str, path: str) -> Operation | None:
    """Find the operation a served (method, path) belongs to.

    Accepts either prefix, and also the prefix-free shape a router declares, so
    the registry can be checked against a route table directly. A request that
    no operation claims is not an error — member administration, uploads
    bookkeeping and the auth routes are served by the same app without being
    product operations.

    Two passes, in this order. The exact pass answers for a template, which is
    what a route table and every registry check hands in. The template pass
    answers for a concrete path, which is what a request carries: a router
    template says ``/libraries/{library_id}/sources`` and the client sent
    ``/api/v1/libraries/<uuid>/sources``. Only the second pass can find the
    operation behind a real URL, and without it every parameterized operation
    resolved to nothing and was closed.
    """
    canonical = canonical_api_path(path)
    if canonical is not None:
        rest = canonical[len(CANONICAL_PREFIX) :]
    else:
        rest = path

    exact = OPERATION_ROUTES.get((method.upper(), rest))
    if exact is not None:
        return exact
    return operation_for_template(method, rest)


#: (method, prefix-free path) -> operation, for O(1) dispatch-time lookup.
OPERATION_ROUTES: Final[dict[tuple[str, str], Operation]] = {op.route_key: op for op in OPERATIONS}


def assert_registry_consistent() -> None:
    """Fail loudly if the registry contradicts itself.

    This is deliberately cheap and total: it is called while the application is
    being built, so a bad declaration is a startup error and not a 500 that
    only one client happens to see.
    """
    duplicates = sorted(
        name for name in OPERATIONS_BY_NAME if sum(1 for op in OPERATIONS if op.name == name) > 1
    )
    if duplicates:
        raise ValueError(f"duplicate operation names: {duplicates}")

    colliding = sorted(
        f"{method} {path}"
        for (method, path) in OPERATION_ROUTES
        if sum(1 for op in OPERATIONS if op.route_key == (method, path)) > 1
    )
    if colliding:
        raise ValueError(f"two operations share one route: {colliding}")

    tools = [op.mcp_tool for op in OPERATIONS if op.mcp_tool is not None]
    if len(tools) != len(set(tools)):
        raise ValueError("one MCP tool is bound to more than one operation")

    for op in OPERATIONS:
        if not op.path.startswith("/"):
            raise ValueError(f"{op.name}: path must be absolute, prefix-free")
        if "{" in op.name or "}" in op.name or " " in op.name:
            raise ValueError(f"{op.name}: operation name must be a plain slug")
        if op.method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"{op.name}: unsupported method {op.method!r}")
        if op.effect is Effect.READ and op.method not in READ_METHODS:
            raise ValueError(f"{op.name}: a read operation cannot use {op.method}")
        if op.effect is Effect.WRITE and op.method in READ_METHODS:
            raise ValueError(f"{op.name}: a write operation cannot use {op.method}")
        if not op.scope:
            raise ValueError(f"{op.name}: an operation must declare its scope")
        if not op.summary:
            raise ValueError(f"{op.name}: an operation must declare a summary")


def unclaimed_routes(inventory: set[tuple[str, str]]) -> list[tuple[str, str]]:
    """Served paths under the canonical prefix that no operation claims.

    ``inventory`` entries carry the FULL canonical path, because that is what a
    route table holds. :data:`OPERATION_ROUTES` is keyed by the prefix-free
    template, because that is what an operation declares. The lookup has to
    strip the prefix first: comparing the two directly is true for every entry,
    which reported all served routes as unclaimed and made this guard unable to
    notice a new one. With the prefix stripped the residual is the honest set
    of protected routes the registry has not absorbed.
    """
    unclaimed = []
    for method, path in inventory:
        if not path.startswith(f"{CANONICAL_PREFIX}/"):
            continue
        key = (method.upper(), path[len(CANONICAL_PREFIX) :])
        if key not in OPERATION_ROUTES:
            unclaimed.append((method, path))
    return sorted(unclaimed)


__all__ = [
    "ALIAS_PREFIX",
    "API_PREFIXES",
    "API_VERSION",
    "CANONICAL_PREFIX",
    "CONTRACT_REVISION",
    "MCP_OPERATIONS",
    "OPERATIONS",
    "OPERATIONS_BY_NAME",
    "OPERATION_ROUTES",
    "PAGINATED_FAMILIES",
    "PAGINATED_OPERATIONS",
    "READ_METHODS",
    "Effect",
    "Operation",
    "Page",
    "assert_registry_consistent",
    "canonical_api_path",
    "operation_for",
    "operation_for_template",
    "unclaimed_routes",
]
