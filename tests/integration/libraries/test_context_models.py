"""C09 — invariants that live in the models, and need no database.

Deliberately not marked ``integration``: these run inside ``just check`` as well
as ``just integration``, because a violated invariant here is a programming
error, not a service failure. They complement the real-PostgreSQL suite rather
than standing in for it — nothing in this file may be cited as evidence that an
access rule works against a real server.

The two things worth having fast are the closed-context rule (a blocked
requirement must not describe the object behind it) and the absence of a
caller-asserted identity field on any request model. The second is the C02 MCP
contract extended to HTTP, and it is the cheapest possible place for a
privilege-escalation bug to hide.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from kb.catalog import libraries as cat_lib
from kb.catalog import projects as cat_proj
from kb.contracts.entities import Grant, Library, Rule
from kb.contracts.enums import LibraryKind, LibraryRole, PublicationStatus

# Field names that would let a caller assert who they are. A request model that
# grows one of these is a privilege-escalation primitive, whether or not
# anything currently reads it.
FORBIDDEN_REQUEST_FIELDS = frozenset(
    {
        "user_id",
        "actor_id",
        "principal_id",
        "account_id",
        "caller_id",
        "requested_by",
        "on_behalf_of",
        "is_admin",
        "bypass_rls",
        "as_user",
    }
)

REQUEST_MODELS = [
    cat_lib.RegisterLibraryType,
    cat_lib.CreateLibrary,
    cat_lib.SetGrant,
    cat_proj.CreateProject,
    cat_proj.LinkLibrary,
    cat_proj.PinRule,
]

# The one request model allowed to name a principal, and the field means "who
# RECEIVES this role", never "who is asking". Written out as an explicit
# exception rather than a special case in the test, so a second model growing a
# principal field is a visible diff and not a silent pass.
ALLOWED_PRINCIPAL_FIELDS: dict[str, set[str]] = {
    "SetGrant": {"principal_id"},
}


def _rule(**over) -> Rule:
    base = {
        "id": uuid.uuid4(),
        "library_id": uuid.uuid4(),
        "version_no": 1,
        "title": "t",
        "when_to_apply": "when",
        "expected_effect": "effect",
        "publication": PublicationStatus.PUBLISHED,
        "actions": ["step"],
    }
    return Rule(**{**base, **over})


def _library(**over) -> Library:
    base = {
        "id": uuid.uuid4(),
        "name": "n",
        "kind": LibraryKind.BRAND,
        "audience_scope": "private",
    }
    return Library(**{**base, **over})


# ------------------------------------------------------- no identity in input


@pytest.mark.parametrize("model", REQUEST_MODELS, ids=lambda m: m.__name__)
def test_no_request_model_can_assert_an_identity(model):
    allowed = ALLOWED_PRINCIPAL_FIELDS.get(model.__name__, set())
    assert not (FORBIDDEN_REQUEST_FIELDS - allowed) & set(model.model_fields), model.model_fields
    assert model.model_config.get("extra") == "forbid", (
        f"{model.__name__} must reject unknown fields, not ignore them"
    )


@pytest.mark.parametrize("model", REQUEST_MODELS, ids=lambda m: m.__name__)
def test_a_request_model_rejects_an_injected_identity_field(model):
    payload = {"definitely_not_a_field": "x"}
    with pytest.raises(ValidationError):
        model(**payload)  # type: ignore[arg-type]


def test_a_grant_names_the_grantee_but_never_the_grantor():
    """The one place a principal in a body is correct.

    A grant must name *who* is being granted to. What it must never carry is
    the grantor: that is the authenticated caller, and a body that can name it
    is a body that can perform the grant as somebody else.
    """
    fields = set(cat_lib.SetGrant.model_fields)
    assert fields == {"principal_id", "role"}
    assert "role" in fields, "a grant without a role says nothing"


# -------------------------------------------------- the closed-context rule


def test_a_blocked_link_may_not_describe_the_library_behind_it():
    library = _library()
    with pytest.raises(ValidationError, match="may not open"):
        cat_proj.LinkedLibrary(
            is_required=True, status="blocked", library_id=library.id, library=library
        )
    with pytest.raises(ValidationError, match="may not open"):
        cat_proj.LinkedLibrary(is_required=True, status="blocked", library_id=library.id)


def test_an_available_link_must_carry_what_it_resolved_to():
    with pytest.raises(ValidationError, match="must carry the library"):
        cat_proj.LinkedLibrary(is_required=True, status="available")

    library = _library()
    resolved = cat_proj.LinkedLibrary(
        is_required=True, status="available", library_id=library.id, library=library
    )
    assert resolved.library.name == "n"


def test_a_link_cannot_mix_two_libraries():
    first, second = _library(), _library()
    with pytest.raises(ValidationError, match="same library"):
        cat_proj.LinkedLibrary(
            is_required=True,
            status="available",
            library_id=first.id,
            library=second,
        )


def test_a_blocked_pin_may_not_carry_rule_text():
    rule = _rule()
    with pytest.raises(ValidationError, match="may not read"):
        cat_proj.PinnedRule(
            version_no=1, is_required=True, priority=0, status="blocked", rule_id=rule.id, rule=rule
        )


def test_context_state_must_agree_with_the_blocked_counts():
    project = _library(kind=LibraryKind.PROJECT)
    link = cat_proj.LinkedLibrary(is_required=True, status="blocked")
    with pytest.raises(ValidationError, match="complete must agree"):
        cat_proj.ProjectContext(
            project=project,
            links=[link],
            pins=[],
            complete=True,
            blocked_link_count=1,
            blocked_pin_count=0,
            context_state="complete",
        )

    honest = cat_proj.ProjectContext(
        project=project,
        links=[link],
        pins=[],
        complete=False,
        blocked_link_count=1,
        blocked_pin_count=0,
        context_state="blocked",
    )
    assert honest.blocked_required_links == 1


def test_the_mcp_result_reports_reasons_and_never_a_closed_name():
    project = _library(kind=LibraryKind.PROJECT, name="p")
    context = cat_proj.ProjectContext(
        project=project,
        links=[cat_proj.LinkedLibrary(is_required=True, status="blocked")],
        pins=[cat_proj.PinnedRule(version_no=3, is_required=True, priority=10, status="blocked")],
        complete=False,
        blocked_link_count=1,
        blocked_pin_count=1,
        context_state="blocked",
    )
    result = context.to_mcp_result()
    assert result.unresolved_dependencies == [
        "linked_library_access_missing",
        "pinned_rule_access_missing",
    ]
    assert result.pinned_rules == []
    payload = result.model_dump_json()
    assert "library_id" not in payload
    assert "version_no" not in payload, "a blocked pin must not travel at all"


def test_a_fully_resolved_context_reports_no_reasons():
    project = _library(kind=LibraryKind.PROJECT)
    context = cat_proj.ProjectContext(
        project=project,
        links=[
            cat_proj.LinkedLibrary(
                is_required=True, status="available", library_id=project.id, library=project
            )
        ],
        pins=[],
        complete=True,
        blocked_link_count=0,
        blocked_pin_count=0,
        context_state="complete",
    )
    assert context.to_mcp_result().unresolved_dependencies == []


# ------------------------------------------------------------- kind vs audience


@pytest.mark.parametrize(
    ("kind", "audience"),
    [
        (LibraryKind.BRAND, "all_invited"),
        (LibraryKind.REFERENCE, "private"),
        (LibraryKind.PROJECT, "all_invited"),
        (LibraryKind.EXPERIENCE, "invited"),
    ],
)
def test_no_combination_of_kind_and_audience_is_refused(kind, audience):
    """Independence means independence in both directions.

    A brand library readable by everyone invited is ordinary; a reference
    library kept private is ordinary. If either raised, the two axes had been
    quietly coupled, and the coupling would be invisible until a user hit it.
    """
    created = cat_lib.CreateLibrary(
        organisation_id=uuid.uuid4(), name="x", kind=kind, audience_scope=audience
    )
    assert created.kind is kind
    assert created.audience_scope == audience


def test_a_library_type_may_have_no_description():
    """Unknown stays unknown. A registry entry with no description is valid."""
    empty = cat_lib.LibraryType(key="runbook", title="Runbook", template_version=1)
    assert empty.description is None
    assert empty.allowed_extra_fields == []
    assert empty.review_process == {}


def test_a_grant_carries_its_own_library():
    grant = Grant(library_id=uuid.uuid4(), principal_id=uuid.uuid4(), role=LibraryRole.CURATOR)
    assert grant.role is LibraryRole.CURATOR
