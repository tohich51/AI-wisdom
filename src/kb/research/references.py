from copy import deepcopy
from uuid import UUID

_INVALID = "invalid_reference_output"


def reference_schema(schema: dict) -> dict:
    if type(schema) is not dict:
        raise ValueError(_INVALID)
    try:
        result = deepcopy(schema)
        definitions = result["$defs"]
        quote = definitions["Quote"]
        properties = quote["properties"]
        fragment_id = properties["fragment_id"]
    except (KeyError, TypeError):
        raise ValueError(_INVALID) from None
    if (
        type(definitions) is not dict
        or type(quote) is not dict
        or type(properties) is not dict
        or type(fragment_id) is not dict
    ):
        raise ValueError(_INVALID)
    quote["properties"] = {"fragment_id": fragment_id}
    quote["required"] = ["fragment_id"]
    quote["additionalProperties"] = False
    return result


def materialize_references(data: dict, fragments: list[dict]) -> dict:
    if type(data) is not dict or type(fragments) is not list or not 1 <= len(fragments) <= 24:
        raise ValueError(_INVALID)
    try:
        context = {}
        for fragment in fragments:
            if type(fragment) is not dict:
                raise ValueError(_INVALID)
            raw_id = fragment["id"]
            text = fragment["text"]
            if type(raw_id) is not str or type(text) is not str:
                raise ValueError(_INVALID)
            if not text.strip() or len(text) > 2000:
                raise ValueError(_INVALID)
            fragment_id = str(UUID(raw_id))
            if fragment_id in context:
                raise ValueError(_INVALID)
            context[fragment_id] = text

        result = deepcopy(data)
        items = result["items"]
        relations = result["relations"]
        if type(items) is not list or type(relations) is not list:
            raise ValueError(_INVALID)
        for entries in (items, relations):
            for entry in entries:
                if type(entry) is not dict:
                    raise ValueError(_INVALID)
                citations = entry["citations"]
                if type(citations) is not list or not 1 <= len(citations) <= 6:
                    raise ValueError(_INVALID)
                materialized = []
                for citation in citations:
                    if type(citation) is not dict or set(citation) != {"fragment_id"}:
                        raise ValueError(_INVALID)
                    raw_id = citation["fragment_id"]
                    if type(raw_id) is not str:
                        raise ValueError(_INVALID)
                    fragment_id = str(UUID(raw_id))
                    if fragment_id not in context:
                        raise ValueError(_INVALID)
                    materialized.append({"fragment_id": fragment_id, "quote": context[fragment_id]})
                entry["citations"] = materialized
        return result
    except (KeyError, TypeError, ValueError):
        raise ValueError(_INVALID) from None
