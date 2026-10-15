def validate_draft(draft: dict, fragments: list[dict], kind: str) -> None:
    fragment_by_id: dict[str, dict] = {}
    source_ids: set[str] = set()

    def fail() -> None:
        raise ValueError("invalid_research_output")

    def check_citations(citations: list[dict]) -> None:
        if not citations:
            fail()
        for citation in citations:
            fragment_id = citation["fragment_id"]
            if fragment_id not in fragment_by_id:
                fail()
            quote = citation["quote"]
            text = fragment_by_id[fragment_id]["text"]
            if not quote.strip() or quote not in text:
                fail()

    try:
        for fragment in fragments:
            fragment_id = fragment["id"]
            if fragment_id in fragment_by_id:
                fail()
            fragment_by_id[fragment_id] = fragment
            source_ids.add(fragment["source_id"])

        items = draft["items"]
        relations = draft["relations"]
        if draft["no_evidence"]:
            if items or relations:
                fail()
            if kind == "comparison" and len(source_ids) < 2:
                fail()
            return None
        elif not items:
            fail()

        covered_sources: set[str] = set()
        for index, item in enumerate(items):
            parent_index = item["parent_index"]
            if kind == "topic":
                if parent_index is not None and (
                    type(parent_index) is not int or parent_index < 0 or parent_index >= index
                ):
                    fail()
            elif parent_index is not None:
                fail()

            source_id = item["source_id"]
            if source_id is not None and source_id not in source_ids:
                fail()

            citations = item["citations"]
            check_citations(citations)
            if source_id is not None:
                for citation in citations:
                    cited_source_id = fragment_by_id[citation["fragment_id"]]["source_id"]
                    if cited_source_id != source_id:
                        fail()
                covered_sources.add(source_id)
            if kind == "comparison" and source_id is None:
                fail()

        if kind == "comparison" and (len(source_ids) < 2 or covered_sources != source_ids):
            fail()

        if relations and kind != "network":
            fail()

        adjacency: list[list[int]] = [[] for _ in items]
        indegree = [0] * len(items)
        relation_keys: set[tuple[int, int, str]] = set()
        predicates = {"supports", "part_of", "used_for", "contradicts"}

        for relation in relations:
            from_item = relation["from_item"]
            to_item = relation["to_item"]
            if (
                type(from_item) is not int
                or type(to_item) is not int
                or not 0 <= from_item < len(items)
                or not 0 <= to_item < len(items)
                or from_item == to_item
            ):
                fail()

            predicate = relation["predicate"]
            if predicate not in predicates:
                fail()
            relation_key = (from_item, to_item, predicate)
            if relation_key in relation_keys:
                fail()
            relation_keys.add(relation_key)

            check_citations(relation["citations"])
            if predicate == "part_of":
                adjacency[from_item].append(to_item)
                indegree[to_item] += 1

        if kind == "network":
            queue = [index for index, degree in enumerate(indegree) if degree == 0]
            cursor = 0
            while cursor < len(queue):
                node = queue[cursor]
                cursor += 1
                for parent in adjacency[node]:
                    indegree[parent] -= 1
                    if indegree[parent] == 0:
                        queue.append(parent)
            if len(queue) != len(items):
                fail()
    except (KeyError, TypeError, AttributeError, IndexError):
        raise ValueError("invalid_research_output") from None

    return None
