"""C02 contract tests.

These assert product invariants, not implementation details: if one of them
starts failing, the product's claims about provenance, identity or immutability
have changed.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kb.contracts import (
    EXPECTED_TOOLS,
    FORBIDDEN_ARG_FIELDS,
    FORBIDDEN_MCP_TOOLS,
    MCP_TOOLS,
    assert_no_identity_injection,
)
from kb.contracts.entities import (
    Experience,
    Locator,
    Provenance,
    Rule,
    Source,
    TrustedContext,
    Use,
)
from kb.contracts.enums import LibraryRole, LocatorKind, PublicationStatus
from kb.domain.tiers import UsageState


def test_all_ten_mcp_tools_are_declared():
    assert set(MCP_TOOLS) == EXPECTED_TOOLS
    assert len(MCP_TOOLS) == 10


def test_no_admin_or_grant_mutation_over_mcp():
    assert not (FORBIDDEN_MCP_TOOLS & set(MCP_TOOLS))


def test_no_tool_argument_carries_identity():
    assert_no_identity_injection()
    for tool, (args, _r) in MCP_TOOLS.items():
        for field in args.model_fields:
            assert field not in FORBIDDEN_ARG_FIELDS, f"{tool}.{field}"


def test_trusted_context_is_not_constructible_from_a_request_model():
    """Identity lives on the server side. No request model has a slot for it."""
    request_like = {
        "principal_id": str(uuid4()),
        "account_id": str(uuid4()),
        "is_admin": True,
    }
    for _tool, (args, _r) in MCP_TOOLS.items():
        with pytest.raises(ValidationError):
            args.model_validate(request_like)


def test_printed_label_is_not_a_file_page():
    with pytest.raises(ValidationError):
        Locator(kind=LocatorKind.PDF_PRINTED_LABEL, file_page=3)


def test_file_page_locator_requires_a_page():
    with pytest.raises(ValidationError):
        Locator(kind=LocatorKind.PDF_FILE_PAGE)


def test_unknown_provenance_stays_none():
    p = Provenance(source_id=uuid4(), author=None, publication=None)
    assert p.author is None and p.publication is None


def test_published_rule_must_be_applicable():
    with pytest.raises(ValidationError):
        Rule(id=uuid4(), library_id=uuid4(), title="t", publication=PublicationStatus.PUBLISHED)


def test_unknown_field_is_refused():
    with pytest.raises(ValidationError):
        Source(
            id=uuid4(),
            library_id=uuid4(),
            title="t",
            submitted_by=uuid4(),
            media_type="text/plain",
            object_key="a/b",
            content_hash="0" * 64,
            surprise_field=1,
        )


def test_experience_pins_an_exact_rule_version():
    """An outcome must be attributable to the version actually used."""
    use = Use(id=uuid4(), rule_version_id=uuid4(), principal_id=uuid4(), state=UsageState.APPLIED)
    exp = Experience(id=uuid4(), use_id=use.id, rule_version_id=use.rule_version_id)
    assert exp.rule_version_id == use.rule_version_id


def test_trusted_context_requires_a_generation_watermark():
    """Reading generation 0 instead of failing is how stale reads happen."""
    with pytest.raises(ValidationError):
        TrustedContext(principal_id=uuid4(), account_id=uuid4())


def test_trusted_context_carries_per_library_roles():
    lib = uuid4()
    tc = TrustedContext(
        principal_id=uuid4(),
        account_id=uuid4(),
        generation_watermark=1,
        roles_by_library={lib: LibraryRole.CURATOR},
    )
    assert tc.roles_by_library[lib] is LibraryRole.CURATOR


def test_contracts_export_stable_json_schema():
    from kb.contracts import entities

    schema = entities.Rule.model_json_schema()
    assert schema["type"] == "object"
    # frozen models must serialise deterministically for the export generator
    a = json.dumps(schema, sort_keys=True)
    b = json.dumps(entities.Rule.model_json_schema(), sort_keys=True)
    assert a == b
