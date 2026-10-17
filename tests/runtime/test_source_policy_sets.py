"""Real source policy-set changes must be visible in the next statement."""

from dataclasses import replace

import psycopg
import pytest
from test_research_grounding import parsed
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import transaction_identity
from kb.catalog import policies

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]


_VISIBLE_IDS = """
SELECT 'source', s.id::text
  FROM kb.source AS s
 WHERE s.library_id = %s
UNION
SELECT 'fragment', f.id::text
  FROM kb.fragment AS f
  JOIN kb.source AS s ON s.id = f.source_id
 WHERE s.library_id = %s
UNION
SELECT 'source_version', sv.id::text
  FROM kb.source_version AS sv
  JOIN kb.source AS s ON s.id = sv.source_id
 WHERE s.library_id = %s
"""


def _visible_ids(conn, library_id):
    rows = conn.execute(_VISIBLE_IDS, (library_id, library_id, library_id)).fetchall()
    return {(kind, identity) for kind, identity in rows}


def _set_reader(conn, world, granted):
    principal = world["other"].principal_id
    with transaction_identity(conn, world["principal"]):
        if granted:
            conn.execute(
                """INSERT INTO kb.library_grant(library_id, principal_id, role)
                   VALUES (%s, %s, 'reader')""",
                (world["lib"], principal),
            )
        else:
            conn.execute(
                """DELETE FROM kb.library_grant
                   WHERE library_id = %s AND principal_id = %s AND role = 'reader'""",
                (world["lib"], principal),
            )
    conn.commit()


def _set_external(conn, world, allowed):
    policies.put_policy(
        conn,
        world["principal"],
        world["lib"],
        policies.LibraryPolicyInput(
            external_processing_allowed=allowed,
            cloud_mcp_allowed=True,
            export_allowed=True,
        ),
    )
    conn.commit()


def test_source_policy_set_is_rebuilt_each_statement(world):
    parsed(world, "A source provides canonical evidence for the prepared policy test.")
    owner = world["principal"]
    other = world["other"]
    cloud_owner = replace(owner, cloud_client=True)

    with psycopg.connect(world["dsn"]) as conn:
        conn.prepare_threshold = 0

        with transaction_identity(conn, owner):
            baseline = _visible_ids(conn, world["lib"])
            assert {"source", "fragment", "source_version"} <= {kind for kind, _ in baseline}
            for _ in range(6):
                assert _visible_ids(conn, world["lib"]) == baseline
        conn.commit()

        with transaction_identity(conn, other):
            assert _visible_ids(conn, world["lib"]) == set()
        conn.commit()

        _set_reader(conn, world, True)
        with transaction_identity(conn, other):
            assert _visible_ids(conn, world["lib"]) == baseline
        conn.commit()

        _set_reader(conn, world, False)
        with transaction_identity(conn, other):
            assert _visible_ids(conn, world["lib"]) == set()
        conn.commit()

        _set_reader(conn, world, True)
        _set_external(conn, world, False)
        with transaction_identity(conn, cloud_owner):
            assert _visible_ids(conn, world["lib"]) == set()
        conn.commit()

        _set_external(conn, world, True)
        with transaction_identity(conn, cloud_owner):
            assert _visible_ids(conn, world["lib"]) == baseline
        conn.commit()


@pytest.mark.parametrize("role", ["reader", "contributor"])
def test_fragment_commands_and_returning_keep_rank_checks(world, role):
    source_id = parsed(world, "Synthetic fragment command evidence.")
    with psycopg.connect(world["dsn"]) as conn, transaction_identity(conn, world["principal"]):
        conn.execute(
            "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,%s)",
            (world["lib"], world["other"].principal_id, role),
        )
        fragment_id = conn.execute(
            "INSERT INTO kb.fragment(source_id,ordinal,text,locator_kind) "
            "VALUES(%s,999998,'Command fixture','none') RETURNING id",
            (source_id,),
        ).fetchone()[0]
    with psycopg.connect(world["dsn"]) as conn:
        if role == "reader":
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction(), transaction_identity(conn, world["other"]):
                    conn.execute(
                        "INSERT INTO kb.fragment(source_id,ordinal,text,locator_kind) "
                        "VALUES(%s,999999,'Denied fixture','none') RETURNING id",
                        (source_id,),
                    ).fetchall()
        else:
            with transaction_identity(conn, world["other"]):
                assert conn.execute(
                    "INSERT INTO kb.fragment(source_id,ordinal,text,locator_kind) "
                    "VALUES(%s,999999,'Allowed fixture','none') RETURNING id",
                    (source_id,),
                ).fetchone()[0]
            conn.commit()
        for statement in [
            "UPDATE kb.fragment SET chapter='Updated' WHERE id=%s RETURNING id",
            "DELETE FROM kb.fragment WHERE id=%s RETURNING id",
        ]:
            if role == "contributor":
                with pytest.raises(
                    psycopg.errors.CheckViolation, match="canonical fragments are immutable"
                ):
                    with conn.transaction(), transaction_identity(conn, world["other"]):
                        conn.execute(statement, (fragment_id,)).fetchall()
            else:
                with transaction_identity(conn, world["other"]):
                    assert conn.execute(statement, (fragment_id,)).fetchall() == []
                conn.commit()
