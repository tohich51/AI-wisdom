"""Pagination behaviour for ``search_knowledge``, over real PostgreSQL.

Two focused tests:

* lexical pages are bounded, disjoint, cover the corpus exactly once, report
  ``has_more`` from the lookahead row, and audit only delivered rows;
* a labelled stand-in semantic service shows every paginated request pays for
  one fixed candidate window and audits only its own slice.

The semantic service is a controlled fake: it proves nothing about provider
recall, ranking quality or the candidate RRF, and the existing semantic ACL and
citation tests stay the authority for that.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import transaction_identity
from kb.catalog import content, libraries

pytestmark = pytest.mark.integration

QUERY = "pagination token"
PAGE_LIMIT = 50
SEMANTIC_PAGE_LIMIT = 10
TOTAL_ROWS = 80


def _seed(conn, principal, library_id, *, count, suffix=""):
    """Insert synthetic draft knowledge rows owned by ``principal``.

    Rows stay ``draft``: publishing without provenance is what this repository
    forbids, and the insert runs inside ``transaction_identity`` so
    ``created_by`` is the owning principal.
    """
    ids = []
    with transaction_identity(conn, principal):
        for index in range(count):
            knowledge_id = uuid4()
            conn.execute(
                """
                INSERT INTO kb.knowledge (id, library_id, statement, kind, publication)
                VALUES (%s, %s, %s, 'assertion', 'draft')
                """,
                (knowledge_id, library_id, f"{QUERY} {index:03d}{suffix}"),
            )
            ids.append(knowledge_id)
    return ids


class _ControlledSemantic:
    """Labelled stand-in for the configured semantic service.

    It reads the RLS-visible rows a real service would finish on and honours
    the requested limit, so pagination is checked deterministically without a
    provider call. Not evidence about real semantic retrieval.
    """

    def __init__(self, knowledge_ids):
        self.knowledge_ids = list(knowledge_ids)
        self.limit_calls = []
        self.ranked_counts = []

    def search(self, conn, principal, query, scopes, limit, **kwargs):
        self.limit_calls.append(limit)
        with (
            transaction_identity(conn, principal),
            conn.cursor(row_factory=dict_row) as cursor,
        ):
            cursor.execute(
                """
                SELECT k.id, k.library_id, k.statement, k.kind, k.publication, k.created_at
                FROM kb.knowledge AS k
                WHERE k.id = ANY(%s::uuid[])
                ORDER BY k.created_at DESC, k.id
                """,
                (self.knowledge_ids,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        self.ranked_counts.append(len(rows))
        return {
            "mode": "semantic",
            "semantic_search_enabled": True,
            "hits": rows[:limit],
            "retrieved_only": True,
        }


def test_lexical_pages_are_bounded_disjoint_and_audit_only_delivered_hits(world):
    principal, dsn, lib = world["principal"], world["dsn"], world["lib"]
    with psycopg.connect(dsn) as conn:
        seeded = _seed(conn, principal, lib, count=TOTAL_ROWS)
        # Same organisation, a different active member's private library, no
        # grant to the caller: only RLS can hide it. No new organisation.
        other_library = libraries.create_library(
            conn,
            world["other"],
            libraries.CreateLibrary(
                organisation_id=principal.account_id, name="Other " + str(uuid4()), kind="reference"
            ),
        )
        foreign = _seed(conn, world["other"], other_library.id, count=3, suffix=" foreign")
        # library_ids=None on purpose: the foreign rows match the query and are
        # excluded by RLS alone, not by an explicit scope argument.
        first = content.search_knowledge(conn, principal, QUERY, None, PAGE_LIMIT, offset=0)
        second = content.search_knowledge(
            conn, principal, QUERY, None, PAGE_LIMIT, offset=PAGE_LIMIT
        )

    first_ids = [hit["id"] for hit in first["hits"]]
    second_ids = [hit["id"] for hit in second["hits"]]

    # 50 + 30 = the 80 seeded rows, delivered once and only once.
    assert len(first_ids) == PAGE_LIMIT
    assert len(second_ids) == TOTAL_ROWS - PAGE_LIMIT
    assert not set(first_ids) & set(second_ids)
    assert set(first_ids) | set(second_ids) == set(seeded)
    assert not set(foreign) & (set(first_ids) | set(second_ids))
    assert first["mode"] == "lexical" and first["semantic_search_enabled"] is False
    assert first["retrieved_only"] is True
    assert first["page"]["has_more"] is True
    assert first["page"]["next_offset"] == PAGE_LIMIT
    assert first["page"]["candidate_window"] is None
    assert second["page"]["offset"] == PAGE_LIMIT
    # The second page reached the end of the corpus, so it promises no more.
    assert second["page"]["has_more"] is False
    assert second["page"]["next_offset"] is None

    # The first page fetched a 51st lookahead row: not delivered, not audited.
    assert len(first["retrieval_event_ids"]) == PAGE_LIMIT
    assert len(second["retrieval_event_ids"]) == TOTAL_ROWS - PAGE_LIMIT
    assert not set(first["retrieval_event_ids"]) & set(second["retrieval_event_ids"])


def test_paginated_semantic_search_pays_for_one_fixed_candidate_window(world):
    principal, dsn, lib = world["principal"], world["dsn"], world["lib"]
    with psycopg.connect(dsn) as conn:
        seeded = _seed(conn, principal, lib, count=TOTAL_ROWS)
    service = _ControlledSemantic(seeded)
    with psycopg.connect(dsn) as conn:
        args = (conn, principal, QUERY, [lib], SEMANTIC_PAGE_LIMIT)
        first = content.search_knowledge(*args, offset=0, semantic=service)
        second = content.search_knowledge(*args, offset=SEMANTIC_PAGE_LIMIT, semantic=service)
        legacy = content.search_knowledge(*args, semantic=service)

    # Both pages used the same fixed window over the same 80 candidates.
    assert service.limit_calls[:2] == [200, 200]
    assert service.ranked_counts[:2] == [TOTAL_ROWS, TOTAL_ROWS]
    assert first["page"]["candidate_window"] == 200
    assert second["page"]["candidate_window"] == 200

    first_ids = [hit["id"] for hit in first["hits"]]
    second_ids = [hit["id"] for hit in second["hits"]]
    assert len(first_ids) == SEMANTIC_PAGE_LIMIT
    assert len(second_ids) == SEMANTIC_PAGE_LIMIT
    assert not set(first_ids) & set(second_ids)
    assert first["page"]["has_more"] is True
    assert first["page"]["next_offset"] == SEMANTIC_PAGE_LIMIT

    # Ten delivered rows audited, not the eighty candidates behind them.
    assert len(first["retrieval_event_ids"]) == SEMANTIC_PAGE_LIMIT
    assert len(second["retrieval_event_ids"]) == SEMANTIC_PAGE_LIMIT
    assert not set(first["retrieval_event_ids"]) & set(second["retrieval_event_ids"])

    # No offset means no pagination at all: legacy budget, no page key.
    assert service.limit_calls[2] == SEMANTIC_PAGE_LIMIT
    assert len(legacy["hits"]) == SEMANTIC_PAGE_LIMIT
    assert "page" not in legacy
