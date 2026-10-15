"""Model references bind to originals; they never become evidence by themselves."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kb.research.model import Draft
from kb.research.references import materialize_references, reference_schema
from kb.research.validation import validate_draft


def fixture():
    ident, source = str(uuid4()), str(uuid4())
    context = [{"id": ident, "source_id": source, "text": "Première ligne.\nDeuxième\u00a0ligne."}]
    data = {
        "title": "Example",
        "no_evidence": False,
        "items": [
            {
                "heading": None,
                "text": "Two lines.",
                "source_id": None,
                "parent_index": None,
                "citations": [{"fragment_id": ident}],
            }
        ],
        "relations": [],
    }
    return context, data


def test_wire_schema_leaves_canonical_schema_and_other_limits_untouched():
    canonical = Draft.model_json_schema()
    original = deepcopy(canonical)
    wire = reference_schema(canonical)
    assert canonical == original
    assert set(wire["$defs"]["Quote"]["properties"]) == {"fragment_id"}
    assert wire["$defs"]["Quote"]["additionalProperties"] is False
    wire["$defs"]["Quote"] = original["$defs"]["Quote"]
    assert wire == original


def test_materialized_quotes_preserve_unicode_and_caller_input():
    context, data = fixture()
    before = deepcopy(data)
    result = materialize_references(data, context)
    assert data == before
    assert result["items"][0]["citations"][0]["quote"] == context[0]["text"]
    canonical = Draft.model_validate(result).model_dump(mode="json")
    validate_draft(canonical, context, "research")


@pytest.mark.parametrize(
    "bad",
    [
        "unknown_id",
        "malformed_id",
        "alias_duplicate_context",
        "quote",
        "source_id",
        "locator",
        "empty_text",
        "blank_text",
        "long_text",
        "no_refs",
    ],
)
def test_reference_rejects_ambiguity_fabrication_and_invalid_context(bad):
    context, data = fixture()
    ref = data["items"][0]["citations"][0]
    if bad == "unknown_id":
        ref["fragment_id"] = str(uuid4())
    elif bad == "malformed_id":
        ref["fragment_id"] = "not-a-uuid"
    elif bad == "alias_duplicate_context":
        context.append({**context[0], "id": context[0]["id"].upper()})
    elif bad in {"quote", "source_id", "locator"}:
        ref[bad] = "Untrusted content"
    elif bad == "empty_text":
        context[0]["text"] = ""
    elif bad == "blank_text":
        context[0]["text"] = " \n\t"
    elif bad == "long_text":
        context[0]["text"] = "a" * 2001
    elif bad == "no_refs":
        data["items"][0]["citations"] = []
    with pytest.raises(ValueError, match=r"^invalid_reference_output$"):
        materialize_references(data, context)


def test_unknown_fields_are_not_dropped_to_bypass_canonical_contract():
    context, data = fixture()
    data["items"][0]["untrusted_override"] = True
    expanded = materialize_references(data, context)
    with pytest.raises(ValidationError):
        Draft.model_validate(expanded)


def test_comparison_reference_still_must_belong_to_declared_source():
    context, data = fixture()
    other = {"id": str(uuid4()), "source_id": str(uuid4()), "text": "Another source."}
    context.append(other)
    data["items"][0]["source_id"] = context[0]["source_id"]
    data["items"][0]["citations"][0]["fragment_id"] = other["id"]
    expanded = Draft.model_validate(materialize_references(data, context)).model_dump(mode="json")
    with pytest.raises(ValueError):
        validate_draft(expanded, context, "comparison")
