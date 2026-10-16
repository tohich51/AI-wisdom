"""Focused real-PostgreSQL regressions for lexical search and retrieval audit."""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.content import lexical_search_knowledge, search_knowledge
from kb.catalog.retrieval_events import record_knowledge_retrievals

pytestmark = pytest.mark.integration

INDEX_NAME = "knowledge_statement_simple_fts_gin"


@pytest.fixture
def catalog(world, admin):
    world.organisation()
    for person in (world.owner, world.colleague):
        admin.execute(
            """
            INSERT INTO kb.membership(
              organisation_id,principal_id,issuer,subject,status
            ) VALUES(%s,%s,%s,%s,'active')
            """,
            (world.account_id, person, "https://b2.invalid", str(person)),
        )
    library_id, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    return world, library_id


def _insert_source(world, library_id, source_id):
    world.conn.execute(
        """
        INSERT INTO kb.source(
          id,library_id,title,media_type,submitted_by,object_key,content_hash
        ) VALUES(%s,%s,'Synthetic B2 source','text/plain',%s,%s,%s)
        """,
        (
            source_id,
            library_id,
            world.owner,
            f"b2/{source_id.hex}",
            source_id.hex * 2,
        ),
    )


@pytest.fixture
def lexical_rows(catalog):
    world, library_id = catalog
    active_a, active_b, withdrawn_source = uuid4(), uuid4(), uuid4()
    for source_id in (active_a, active_b, withdrawn_source):
        _insert_source(world, library_id, source_id)
    world.conn.execute(
        "UPDATE kb.source SET publication='withdrawn' WHERE id=%s",
        (withdrawn_source,),
    )

    tied = tuple(sorted(world.knowledge(library_id, title="b2lex alpha") for _ in range(3)))
    older = world.knowledge(library_id, title="b2lex alpha")
    excluded_by_withdrawn_source = world.knowledge(library_id, title="b2lex alpha")
    withdrawn_knowledge = world.knowledge(library_id, title="b2lex alpha", publication="withdrawn")

    older_time = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    newer_time = older_time + dt.timedelta(seconds=1)
    for item_id in tied:
        world.conn.execute(
            "UPDATE kb.knowledge SET created_at=%s WHERE id=%s",
            (newer_time, item_id),
        )
    world.conn.execute(
        "UPDATE kb.knowledge SET created_at=%s WHERE id=%s",
        (older_time, older),
    )
    world.conn.execute(
        "UPDATE kb.knowledge SET created_at=%s WHERE id=%s",
        (newer_time + dt.timedelta(seconds=1), excluded_by_withdrawn_source),
    )
    world.conn.execute(
        "UPDATE kb.knowledge SET created_at=%s WHERE id=%s",
        (newer_time + dt.timedelta(seconds=2), withdrawn_knowledge),
    )

    for item_id in tied:
        world.conn.execute(
            """
            INSERT INTO kb.knowledge_provenance(knowledge_id,source_id)
            VALUES(%s,%s)
            """,
            (item_id, active_a),
        )
    world.conn.execute(
        """
        INSERT INTO kb.knowledge_provenance(knowledge_id,source_id)
        VALUES(%s,%s)
        """,
        (older, active_b),
    )
    for source_id in (active_a, withdrawn_source):
        world.conn.execute(
            """
            INSERT INTO kb.knowledge_provenance(knowledge_id,source_id)
            VALUES(%s,%s)
            """,
            (excluded_by_withdrawn_source, source_id),
        )

    return {
        "active_a": active_a,
        "active_b": active_b,
        "tied": tied,
        "older": older,
    }


def test_lexical_score_tie_order_and_source_scope(catalog, lexical_rows, gateway):
    world, library_id = catalog
    expected = [*lexical_rows["tied"], lexical_rows["older"]]

    unscoped = lexical_search_knowledge(gateway, world.reader, "b2lex", [library_id])
    assert [hit["id"] for hit in unscoped["hits"]] == expected

    with transaction_identity(gateway, world.reader):
        score_row = gateway.execute(
            """
            SELECT ts_rank_cd(
              to_tsvector('simple', statement),
              plainto_tsquery('simple', %s)
            )
            FROM kb.knowledge
            WHERE id=%s
            """,
            ("b2lex", lexical_rows["tied"][0]),
        ).fetchone()
    assert score_row is not None and score_row[0] > 0
    assert all(hit["score"] == score_row[0] for hit in unscoped["hits"])

    active_a = lexical_search_knowledge(
        gateway,
        world.reader,
        "b2lex",
        [library_id],
        source_ids=[lexical_rows["active_a"]],
    )
    assert [hit["id"] for hit in active_a["hits"]] == list(lexical_rows["tied"])
    assert all(
        [row["source_id"] for row in hit["provenance"]] == [lexical_rows["active_a"]]
        for hit in active_a["hits"]
    )

    active_b = lexical_search_knowledge(
        gateway,
        world.reader,
        "b2lex",
        [library_id],
        source_ids=[lexical_rows["active_b"]],
    )
    assert [hit["id"] for hit in active_b["hits"]] == [lexical_rows["older"]]
    assert (
        lexical_search_knowledge(
            gateway, world.reader, "b2lex", [library_id], source_ids=[uuid4()]
        )["hits"]
        == []
    )
    assert lexical_search_knowledge(gateway, world.reader, "b2lex", [uuid4()])["hits"] == []


def _plan_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _plan_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _plan_nodes(child)


@pytest.fixture
def fts_corpus(catalog):
    world, library_id = catalog
    world.conn.execute(
        """
        INSERT INTO kb.knowledge(library_id,statement,kind,publication)
        SELECT %s,
               'b2background corpus token ' || series_number::text,
               'assertion','published'
        FROM generate_series(1, 6000) AS series(series_number)
        """,
        (library_id,),
    )
    rare = world.knowledge(library_id, title="b2ftsneedle b2background corpus token rare")
    return world, library_id, rare


def test_fts_expression_and_actual_rls_plan(fts_corpus, gateway, record_property):
    world, library_id, rare = fts_corpus
    metadata = world.conn.execute(
        """
        SELECT pg_get_expr(i.indexprs, i.indrelid),
               i.indisvalid, i.indisready, am.amname
        FROM pg_index AS i
        JOIN pg_class AS c ON c.oid=i.indexrelid
        JOIN pg_namespace AS n ON n.oid=c.relnamespace
        JOIN pg_am AS am ON am.oid=c.relam
        WHERE n.nspname='kb' AND c.relname=%s
        """,
        (INDEX_NAME,),
    ).fetchall()
    assert metadata == [
        (
            "to_tsvector('simple'::regconfig, statement)",
            True,
            True,
            "gin",
        )
    ]

    result = lexical_search_knowledge(gateway, world.reader, "b2ftsneedle", [library_id])
    assert [hit["id"] for hit in result["hits"]] == [rare]

    with transaction_identity(gateway, world.reader):
        role_row = gateway.execute("SELECT current_user, session_user").fetchone()
        explain_row = gateway.execute(
            """
            EXPLAIN (FORMAT JSON, COSTS OFF)
            WITH request AS (
              SELECT plainto_tsquery('simple', %s) AS tsquery
            )
            SELECT k.id,
                   ts_rank_cd(
                     to_tsvector('simple', k.statement), request.tsquery
                   ) AS score
            FROM request
            CROSS JOIN kb.knowledge AS k
            WHERE request.tsquery IS NOT NULL
              AND k.publication<>'withdrawn'
              AND to_tsvector('simple', k.statement) @@ request.tsquery
              AND k.library_id=ANY(%s::uuid[])
            ORDER BY score DESC, k.created_at DESC, k.id
            LIMIT 10
            """,
            ("b2ftsneedle", [library_id]),
        ).fetchone()

    assert role_row is not None
    actual_role, session_role = role_row
    assert actual_role == "kb_app"
    assert explain_row is not None
    plan_document = explain_row[0]
    if isinstance(plan_document, str):
        plan_document = json.loads(plan_document)
    plan_nodes = list(_plan_nodes(plan_document))
    plan_text = json.dumps(plan_document, sort_keys=True)
    index_names = [node["Index Name"] for node in plan_nodes if "Index Name" in node]

    record_property(
        "fts_explain_identity",
        {"current_user": actual_role, "session_user": session_role},
    )
    record_property("fts_explain_actual_rls_plan", plan_text)
    record_property("fts_explain_index_names", index_names)
    record_property("fts_explain_uses_migration_index", INDEX_NAME in index_names)
    assert plan_nodes

    # This is deliberately the real kb_app/RLS plan, not an owner plan or a
    # forced-index demonstration. Policy ordering may legitimately yield a
    # sequential scan even though the catalog expression is GIN-compatible.


def test_batch_mapping_duplicates_empty_and_stale(catalog, gateway):
    world, library_id = catalog
    first = world.knowledge(library_id, title="B2 batch first")
    second = world.knowledge(library_id, title="B2 batch second")
    requested = (second, first, first)

    with transaction_identity(gateway, world.manager):
        baseline = gateway.execute(
            """
            SELECT count(*)
            FROM kb.retrieval_event
            WHERE principal_id=%s
            """,
            (world.manager.principal_id,),
        ).fetchone()[0]
        with gateway.cursor(row_factory=dict_row) as cursor:
            assert record_knowledge_retrievals(cursor, world.manager, item_ids=()) == []
            assert (
                gateway.execute(
                    """
                SELECT count(*)
                FROM kb.retrieval_event
                WHERE principal_id=%s
                """,
                    (world.manager.principal_id,),
                ).fetchone()[0]
                == baseline
            )

            event_ids = record_knowledge_retrievals(cursor, world.manager, item_ids=requested)

        rows = gateway.execute(
            """
            SELECT id,item_id
            FROM kb.retrieval_event
            WHERE id=ANY(%s::uuid[])
            """(
                event_ids,
            ),
        ).fetchall()

    item_by_event = {event_id: item_id for event_id, item_id in rows}
    assert len(event_ids) == len(set(event_ids)) == len(requested)
    assert [item_by_event[event_id] for event_id in event_ids] == list(requested)
    assert sum(item_id == first for item_id in item_by_event.values()) == 2

    stale = Principal(
        principal_id=world.manager.principal_id,
        account_id=world.manager.account_id,
        generation_watermark=0,
    )
    with transaction_identity(gateway, world.manager):
        with gateway.cursor(row_factory=dict_row) as cursor:
            with pytest.raises(AccessDenied):
                record_knowledge_retrievals(cursor, stale, item_ids=requested)
        assert gateway.execute(
            """
            SELECT count(*)
            FROM kb.retrieval_event
            WHERE principal_id=%s
            """,
            (world.manager.principal_id,),
        ).fetchone()[0] == baseline + len(requested)


def test_batch_denial_rolls_back_prefix_inside_caught_outer_transaction(catalog, gateway):
    world, library_id = catalog
    valid_before = world.knowledge(library_id, title="B2 valid before denial")
    valid_after = world.knowledge(library_id, title="B2 valid after denial")
    withdrawn = world.knowledge(library_id, title="B2 withdrawn knowledge", publication="withdrawn")

    invisible_library = world.library(name="B2 invisible library")
    invisible = world.knowledge(invisible_library, title="B2 invisible knowledge")

    foreign_account = uuid4()
    world.conn.execute(
        "INSERT INTO kb.organisation(id,name) VALUES(%s,%s)",
        (foreign_account, f"b2-foreign-{foreign_account.hex[:8]}"),
    )
    foreign_library = uuid4()
    world.conn.execute(
        """
        INSERT INTO kb.library(
          id,organisation_id,name,kind,audience_scope
        ) VALUES(%s,%s,%s,'reference','private')
        """,
        (
            foreign_library,
            foreign_account,
            f"b2-foreign-{foreign_library.hex[:8]}",
        ),
    )
    world.grant(foreign_library, world.owner, "manager")
    foreign = world.knowledge(foreign_library, title="B2 foreign knowledge")

    denials = (
        ("missing", uuid4()),
        ("invisible", invisible),
        ("foreign", foreign),
        ("withdrawn", withdrawn),
    )
    baseline = world.conn.execute(
        """
        SELECT count(*)
        FROM kb.retrieval_event
        WHERE principal_id=%s
        """,
        (world.manager.principal_id,),
    ).fetchone()[0]

    with transaction_identity(gateway, world.manager):
        baseline_visible = gateway.execute(
            """
            SELECT count(*)
            FROM kb.retrieval_event
            WHERE principal_id=%s
            """,
            (world.manager.principal_id,),
        ).fetchone()[0]
        with gateway.cursor(row_factory=dict_row) as cursor:
            for label, denied_id in denials:
                with pytest.raises(AccessDenied):
                    record_knowledge_retrievals(
                        cursor,
                        world.manager,
                        item_ids=(valid_before, denied_id, valid_after),
                    )
                assert cursor.execute("SELECT 1").fetchone() is not None
                assert (
                    gateway.execute(
                        """
                    SELECT count(*)
                    FROM kb.retrieval_event
                    WHERE principal_id=%s
                    """,
                        (world.manager.principal_id,),
                    ).fetchone()[0]
                    == baseline_visible
                ), label

            survivor = record_knowledge_retrievals(cursor, world.manager, item_ids=(valid_after,))
            assert len(survivor) == 1

    assert (
        world.conn.execute(
            """
        SELECT count(*)
        FROM kb.retrieval_event
        WHERE principal_id=%s
        """,
            (world.manager.principal_id,),
        ).fetchone()[0]
        == baseline + 1
    )


@pytest.mark.parametrize("detached", [False, True], ids=["direct", "detached-factory"])
def test_shared_search_maps_response_events_on_both_paths(catalog, gateway, gateway_dsn, detached):
    world, library_id = catalog
    for number in range(3):
        world.knowledge(library_id, title=f"b2shared {number}")

    @contextmanager
    def factory():
        with psycopg.connect(gateway_dsn, autocommit=True) as conn:
            yield conn

    result = search_knowledge(
        None if detached else gateway,
        world.reader,
        "b2shared",
        [library_id],
        3,
        connections=factory if detached else None,
    )
    assert result["mode"] == "lexical"
    assert result["semantic_search_enabled"] is False
    assert result["retrieved_only"] is True
    assert len(result["hits"]) == len(result["retrieval_event_ids"]) == 3
    assert len(set(result["retrieval_event_ids"])) == 3

    with transaction_identity(gateway, world.reader):
        event_rows = gateway.execute(
            """
            SELECT id,item_id
            FROM kb.retrieval_event
            WHERE id=ANY(%s::uuid[])
            """,
            (result["retrieval_event_ids"],),
        ).fetchall()
    item_by_event = dict(event_rows)
    assert [item_by_event[event_id] for event_id in result["retrieval_event_ids"]] == [
        hit["id"] for hit in result["hits"]
    ]
