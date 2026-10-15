from uuid import UUID, uuid4

import psycopg
import pytest
from test_runtime import rule, source
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import transaction_identity
from kb.catalog import graphs, writes

pytestmark = pytest.mark.integration


def test_provenance_prioritizes_visible_support_over_raw_fragment_budget(world):
    source_a = source(world, "Source A public fixture text.")
    source_b = source(world, "Source B public fixture text.")
    low_bits = (1 << 120) - 1
    raw_fragment_ids = [UUID(int=(uuid4().int & low_bits)) for _ in range(202)]
    support_fragment = UUID(int=(0xFF << 120) | (uuid4().int & low_bits))

    with (
        psycopg.connect(world["dsn"]) as conn,
        transaction_identity(conn, world["principal"]),
        conn.cursor() as cur,
    ):
        cur.executemany(
            "INSERT INTO kb.fragment(id,source_id,locator_kind,ordinal,text) "
            "VALUES(%s,%s,'none',%s,%s)",
            [
                (fragment_id, source_a, ordinal, "Public fixture fragment.")
                for ordinal, fragment_id in enumerate(raw_fragment_ids, start=1)
            ],
        )
        cur.execute(
            "INSERT INTO kb.fragment(id,source_id,locator_kind,ordinal,text) "
            "VALUES(%s,%s,'none',1,%s)",
            (support_fragment, source_b, "Public fixture support fragment."),
        )

    knowledge_id = uuid4()
    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            conn.execute(
                "INSERT INTO kb.knowledge(id,library_id,statement,kind) "
                "VALUES(%s,%s,%s,'definition')",
                (knowledge_id, world["lib"], "Published knowledge supported by the fixture."),
            )
            conn.execute(
                "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id,fragment_id) "
                "VALUES(%s,%s,%s)",
                (knowledge_id, source_b, support_fragment),
            )
        writes.publish(conn, world["principal"], knowledge_id, knowledge=True)

    published_rule = rule(world, support_fragment)
    rule_id = published_rule["id"]

    with psycopg.connect(world["dsn"]) as conn:
        graph = graphs.provenance_graph(conn, world["principal"], world["lib"])
    assert len(graph["nodes"]) <= 200
    assert graph["truncated"] is True
    assert [node for node in graph["nodes"] if node["entity_id"] == support_fragment] == [
        {
            "id": f"fragment:{support_fragment}",
            "entity_id": support_fragment,
            "kind": "fragment",
            "label": "Public fixture support fragment.",
            "library_id": world["lib"],
            "publication": "original",
            "version_no": None,
        }
    ]

    node_ids = {node["id"] for node in graph["nodes"]}
    assert all(edge["source"] in node_ids and edge["target"] in node_ids for edge in graph["edges"])
    assert not {node["kind"] for node in graph["nodes"]} & {"use", "outcome"}
    assert not {"retrieved", "used", "reports"} & {edge["predicate"] for edge in graph["edges"]}

    contains = {
        "id": f"contains:{source_b}:{support_fragment}",
        "source": f"source:{source_b}",
        "target": f"fragment:{support_fragment}",
        "predicate": "contains",
        "basis": "canonical",
        "evidence": {
            "id": support_fragment,
            "source_id": source_b,
            "locator_kind": "none",
            "file_page": None,
            "chapter": None,
            "paragraph": None,
            "ordinal": 1,
        },
    }
    assert contains in graph["edges"]
    for kind, entity_id in (("knowledge", knowledge_id), ("rule", rule_id)):
        assert {
            "id": f"grounds:{support_fragment}:{entity_id}",
            "source": f"fragment:{support_fragment}",
            "target": f"{kind}:{entity_id}",
            "predicate": "grounds",
            "basis": "recorded_provenance",
            "evidence": {
                "source_id": source_b,
                "fragment_id": support_fragment,
                "entity": entity_id,
            },
        } in graph["edges"]

    with psycopg.connect(world["dsn"]) as conn:
        scoped = graphs.provenance_graph(conn, world["principal"], world["lib"], [source_a])
    scoped_entity_ids = {node["entity_id"] for node in scoped["nodes"]}
    assert {source_b, support_fragment, knowledge_id, rule_id}.isdisjoint(scoped_entity_ids)
    assert all(
        edge["source"] in {node["id"] for node in scoped["nodes"]}
        and edge["target"] in {node["id"] for node in scoped["nodes"]}
        for edge in scoped["edges"]
    )
