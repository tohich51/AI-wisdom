"""Real PostgreSQL equivalence and bounded-regression gates for manifest reads."""

import copy
import hashlib
import json
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_runtime import runtime_db as runtime_db
from test_runtime import source as source
from test_runtime import world as world

from kb.access.policy import transaction_identity
from kb.jobs.ingestion import run_job, submit_processing
from kb.research.grounding import capture_snapshot

pytestmark = pytest.mark.integration


# Exact legacy body under a connection-local test-only name. It deliberately
# retains its per-item SQL queries so every new semantic assertion is an
# equivalence assertion rather than a duplicated expected-result table.
_REFERENCE_SQL = """
CREATE OR REPLACE FUNCTION pg_temp.can_read_manifest_reference(p_manifest jsonb)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,kb AS $$
DECLARE item jsonb; source_ids uuid[]:='{}'; fragment_ids uuid[]:='{}';
 source_id uuid; version_id uuid; fragment_id uuid; start_at integer; end_at integer;
 visible_source uuid; visible_version uuid; source_hash text; version_hash text;
 publication text; canonical text; fragment_source uuid;
BEGIN
 IF jsonb_typeof(p_manifest) IS DISTINCT FROM 'object'
  OR jsonb_typeof(p_manifest->'sources') IS DISTINCT FROM 'array'
  OR jsonb_typeof(p_manifest->'fragments') IS DISTINCT FROM 'array' THEN RETURN false; END IF;
 IF jsonb_array_length(p_manifest->'sources') NOT BETWEEN 1 AND 10
  OR jsonb_array_length(p_manifest->'fragments') NOT BETWEEN 1 AND 24 THEN RETURN false; END IF;
 FOR item IN SELECT value FROM jsonb_array_elements(p_manifest->'sources') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object'
   OR jsonb_typeof(item->'source_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'source_version_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'content_hash') IS DISTINCT FROM 'string' THEN RETURN false; END IF;
  source_id:=(item->>'source_id')::uuid; version_id:=(item->>'source_version_id')::uuid;
  IF source_id IS NULL OR version_id IS NULL OR source_id=ANY(source_ids) THEN RETURN false; END IF;
  SELECT s.id,v.id,s.content_hash,v.content_hash,s.publication::text
  INTO visible_source,visible_version,source_hash,version_hash,publication
  FROM (SELECT source_id AS id) requested
  LEFT JOIN kb.source s ON s.id=requested.id
  LEFT JOIN kb.source_version v ON v.id=version_id AND v.source_id=s.id;
  IF visible_source IS NULL OR visible_version IS NULL OR publication='withdrawn'
    OR source_hash IS DISTINCT FROM item->>'content_hash'
    OR version_hash IS DISTINCT FROM item->>'content_hash' THEN RETURN false; END IF;
  source_ids:=array_append(source_ids,source_id);
 END LOOP;
 FOR item IN SELECT value FROM jsonb_array_elements(p_manifest->'fragments') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object'
   OR jsonb_typeof(item->'fragment_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'source_id') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'text_hash') IS DISTINCT FROM 'string'
   OR jsonb_typeof(item->'excerpt_start') IS DISTINCT FROM 'number'
   OR jsonb_typeof(item->'excerpt_end') IS DISTINCT FROM 'number' THEN RETURN false; END IF;
  fragment_id:=(item->>'fragment_id')::uuid; source_id:=(item->>'source_id')::uuid;
  start_at:=(item->>'excerpt_start')::integer; end_at:=(item->>'excerpt_end')::integer;
  IF fragment_id IS NULL OR source_id IS NULL OR fragment_id=ANY(fragment_ids)
   OR NOT source_id=ANY(source_ids) OR start_at IS NULL OR end_at IS NULL THEN RETURN false; END IF;
  SELECT f.text,f.source_id INTO canonical,fragment_source
  FROM (SELECT fragment_id AS id) requested LEFT JOIN kb.fragment f ON f.id=requested.id;
  IF canonical IS NULL OR fragment_source IS DISTINCT FROM source_id
   OR start_at<0 OR end_at<=start_at OR end_at>char_length(canonical)
   OR encode(sha256(convert_to(canonical,'UTF8')),'hex') IS DISTINCT FROM item->>'text_hash'
   THEN RETURN false; END IF;
  fragment_ids:=array_append(fragment_ids,fragment_id);
 END LOOP;
 IF EXISTS(SELECT 1 FROM unnest(source_ids) selected WHERE NOT EXISTS(
    SELECT 1 FROM jsonb_array_elements(p_manifest->'fragments') f
    WHERE (f->>'source_id')::uuid=selected)) THEN RETURN false; END IF;
 RETURN true;
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN false;
END $$;
"""

_SQL_NULL = object()


def parsed(world, text):
    ident = source(world, text)
    with psycopg.connect(world["dsn"]) as conn:
        job = submit_processing(conn, world["principal"], ident, str(uuid4()))
    run_job(world["worker"], job["job_id"], world["principal"], world["store"].root)
    return ident


def _install_reference(conn):
    # Creating an object first ensures the connection has a pg_temp namespace.
    conn.execute("CREATE TEMP TABLE manifest_reference_bootstrap (id integer)")
    conn.execute(_REFERENCE_SQL)
    conn.execute("REVOKE ALL ON FUNCTION pg_temp.can_read_manifest_reference(jsonb) FROM PUBLIC")


def _encode(value):
    return None if value is _SQL_NULL else Jsonb(value)


def _result(conn, value, label, expected=None):
    encoded = _encode(value)
    reference, current = conn.execute(
        """SELECT pg_temp.can_read_manifest_reference(%s), kb.can_read_manifest(%s)""",
        (encoded, encoded),
    ).fetchone()
    assert reference == current, f"{label}: reference={reference}, current={current}"
    if expected is not None:
        assert current == expected, (
            f"{label}: expected={expected}, reference={reference}, current={current}"
        )
    return current


def _section(manifest, section, entries):
    result = copy.deepcopy(manifest)
    result[section] = copy.deepcopy(entries)
    return result


def _change(manifest, section, field, value, index=0):
    result = copy.deepcopy(manifest)
    result[section][index][field] = value
    return result


def _without(manifest, section, field, index=0):
    result = copy.deepcopy(manifest)
    del result[section][index][field]
    return result


def test_manifest_matches_legacy_reference_matrix(world):
    first = parsed(world, "First private source has one measurable sentence.")
    second = parsed(world, "Second private source has a different measurable sentence.")

    with psycopg.connect(world["dsn"]) as conn:
        first_snapshot = capture_snapshot(conn, world["principal"], [first])
        second_snapshot = capture_snapshot(conn, world["principal"], [second])
        valid = {
            "sources": [
                *copy.deepcopy(first_snapshot["manifest"]["sources"]),
                *copy.deepcopy(second_snapshot["manifest"]["sources"]),
            ],
            "fragments": [
                *copy.deepcopy(first_snapshot["manifest"]["fragments"]),
                *copy.deepcopy(second_snapshot["manifest"]["fragments"]),
            ],
        }
        assert len(valid["sources"]) == 2
        assert valid["fragments"]

        first_source = valid["sources"][0]
        second_source = valid["sources"][1]
        first_source_id = first_source["source_id"]
        second_source_id = second_source["source_id"]
        first_fragment = valid["fragments"][0]
        first_fragment_id = first_fragment["fragment_id"]

        augmented = copy.deepcopy(valid)
        for entry in augmented["sources"] + augmented["fragments"]:
            entry["ignored_by_contract"] = {"still": "allowed"}

        reordered = {
            "sources": list(reversed(valid["sources"])),
            "fragments": list(reversed(valid["fragments"])),
        }
        uncovered = _section(
            valid,
            "fragments",
            [item for item in valid["fragments"] if item["source_id"] != second_source_id],
        )
        mismatched_fragments = copy.deepcopy(valid["fragments"])
        mismatch_index = next(
            index
            for index, item in enumerate(mismatched_fragments)
            if item["source_id"] == second_source_id
        )
        mismatched_fragments[mismatch_index]["source_id"] = first_source_id

        cases = [
            ("valid", valid, True),
            ("valid_with_unknown_fields", augmented, True),
            ("valid_reordered", reordered, True),
            ("sql_null", _SQL_NULL, False),
            ("json_null", None, False),
            ("top_level_array", [], False),
            ("top_level_scalar", "manifest", False),
            ("missing_sources", {"fragments": valid["fragments"]}, False),
            ("missing_fragments", {"sources": valid["sources"]}, False),
            (
                "sources_not_array",
                {"sources": {}, "fragments": valid["fragments"]},
                False,
            ),
            (
                "fragments_not_array",
                {"sources": valid["sources"], "fragments": {}},
                False,
            ),
            ("empty_sources", _section(valid, "sources", []), False),
            ("empty_fragments", _section(valid, "fragments", []), False),
            (
                "source_limit",
                _section(valid, "sources", [first_source] * 11),
                False,
            ),
            (
                "fragment_limit",
                _section(valid, "fragments", [first_fragment] * 25),
                False,
            ),
            ("source_item_null", _section(valid, "sources", [None]), False),
            (
                "source_required_field_missing",
                _without(valid, "sources", "source_id"),
                False,
            ),
            (
                "source_id_wrong_type",
                _change(valid, "sources", "source_id", 7),
                False,
            ),
            (
                "source_id_json_null",
                _change(valid, "sources", "source_id", None),
                False,
            ),
            (
                "source_id_invalid_uuid",
                _change(valid, "sources", "source_id", "not-a-uuid"),
                False,
            ),
            (
                "source_hash_wrong_type",
                _change(valid, "sources", "content_hash", 7),
                False,
            ),
            (
                "duplicate_source",
                _section(valid, "sources", [first_source, first_source]),
                False,
            ),
            (
                "missing_source",
                _change(valid, "sources", "source_id", str(uuid4())),
                False,
            ),
            (
                "missing_version",
                _change(valid, "sources", "source_version_id", str(uuid4())),
                False,
            ),
            (
                "cross_source_version",
                _change(
                    valid,
                    "sources",
                    "source_version_id",
                    second_source["source_version_id"],
                ),
                False,
            ),
            (
                "source_content_hash_mismatch",
                _change(valid, "sources", "content_hash", "b" * 64),
                False,
            ),
            ("fragment_item_null", _section(valid, "fragments", [None]), False),
            (
                "fragment_required_field_missing",
                _without(valid, "fragments", "fragment_id"),
                False,
            ),
            (
                "fragment_id_wrong_type",
                _change(valid, "fragments", "fragment_id", 7),
                False,
            ),
            (
                "fragment_id_json_null",
                _change(valid, "fragments", "fragment_id", None),
                False,
            ),
            (
                "fragment_id_invalid_uuid",
                _change(valid, "fragments", "fragment_id", "not-a-uuid"),
                False,
            ),
            (
                "fragment_missing",
                _change(valid, "fragments", "fragment_id", str(uuid4())),
                False,
            ),
            (
                "fragment_source_wrong_type",
                _change(valid, "fragments", "source_id", 7),
                False,
            ),
            (
                "fragment_source_json_null",
                _change(valid, "fragments", "source_id", None),
                False,
            ),
            (
                "fragment_source_invalid_uuid",
                _change(valid, "fragments", "source_id", "not-a-uuid"),
                False,
            ),
            (
                "fragment_source_not_selected",
                _change(valid, "fragments", "source_id", str(uuid4())),
                False,
            ),
            (
                "fragment_source_mismatch",
                _section(valid, "fragments", mismatched_fragments),
                False,
            ),
            (
                "fragment_hash_wrong_type",
                _change(valid, "fragments", "text_hash", 7),
                False,
            ),
            (
                "fragment_hash_mismatch",
                _change(valid, "fragments", "text_hash", "c" * 64),
                False,
            ),
            (
                "start_wrong_type",
                _change(valid, "fragments", "excerpt_start", "0"),
                False,
            ),
            (
                "start_json_null",
                _change(valid, "fragments", "excerpt_start", None),
                False,
            ),
            (
                "negative_start",
                _change(valid, "fragments", "excerpt_start", -1),
                False,
            ),
            (
                "start_overflows_integer",
                _change(valid, "fragments", "excerpt_start", 2_147_483_648),
                False,
            ),
            (
                "empty_bounds",
                _change(
                    valid,
                    "fragments",
                    "excerpt_end",
                    first_fragment["excerpt_start"],
                ),
                False,
            ),
            (
                "end_beyond_text",
                _change(valid, "fragments", "excerpt_end", 2_147_483_647),
                False,
            ),
            (
                "end_overflows_integer",
                _change(valid, "fragments", "excerpt_end", 2_147_483_648),
                False,
            ),
            (
                "end_json_null",
                _change(valid, "fragments", "excerpt_end", None),
                False,
            ),
            (
                "duplicate_fragment",
                _section(
                    valid,
                    "fragments",
                    [first_fragment, *valid["fragments"]],
                ),
                False,
            ),
            ("selected_source_without_fragment", uncovered, False),
        ]

        _install_reference(conn)
        with transaction_identity(conn, world["principal"]):
            for label, manifest, expected in cases:
                _result(conn, manifest, label, expected)

        # Disabling generation does not revoke a previously captured immutable
        # dependency. The legacy predicate and replacement must remain readable.
        with psycopg.connect(world["admin"]) as admin:
            original_generation = admin.execute(
                "SELECT generation_allowed FROM kb.generation_policy WHERE library_id=%s",
                (world["lib"],),
            ).fetchone()[0]
            admin.execute(
                "UPDATE kb.generation_policy SET generation_allowed=false WHERE library_id=%s",
                (world["lib"],),
            )
        try:
            with transaction_identity(conn, world["principal"]):
                _result(conn, valid, "generation_policy_disabled", True)
        finally:
            with psycopg.connect(world["admin"]) as admin:
                admin.execute(
                    "UPDATE kb.generation_policy SET generation_allowed=%s WHERE library_id=%s",
                    (original_generation, world["lib"]),
                )

        # A second active organisation member is private until an explicit
        # reader grant exists, and revoking that grant closes the whole manifest.
        with psycopg.connect(world["admin"]) as admin:
            admin.execute(
                """INSERT INTO kb.library_grant(library_id,principal_id,role)
                   VALUES(%s,%s,'reader')""",
                (world["lib"], world["other"].principal_id),
            )
        try:
            with transaction_identity(conn, world["other"]):
                _result(conn, valid, "private_reader_granted", True)
        finally:
            with psycopg.connect(world["admin"]) as admin:
                admin.execute(
                    """DELETE FROM kb.library_grant
                       WHERE library_id=%s AND principal_id=%s""",
                    (world["lib"], world["other"].principal_id),
                )
        with transaction_identity(conn, world["other"]):
            _result(conn, valid, "private_reader_revoked", False)

        with psycopg.connect(world["admin"]) as admin:
            original_publication = admin.execute(
                "SELECT publication::text FROM kb.source WHERE id=%s", (first,)
            ).fetchone()[0]
            admin.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (first,))
        try:
            with transaction_identity(conn, world["principal"]):
                _result(conn, valid, "source_withdrawn", False)
        finally:
            with psycopg.connect(world["admin"]) as admin:
                admin.execute(
                    "UPDATE kb.source SET publication=%s WHERE id=%s",
                    (original_publication, first),
                )

        with psycopg.connect(world["admin"]) as admin:
            original_role = admin.execute(
                """SELECT role::text FROM kb.library_grant
                   WHERE library_id=%s AND principal_id=%s""",
                (world["lib"], world["principal"].principal_id),
            ).fetchone()[0]
            admin.execute(
                """DELETE FROM kb.library_grant
                   WHERE library_id=%s AND principal_id=%s""",
                (world["lib"], world["principal"].principal_id),
            )
        try:
            with transaction_identity(conn, world["principal"]):
                _result(conn, valid, "owner_library_grant_revoked", False)
        finally:
            with psycopg.connect(world["admin"]) as admin:
                admin.execute(
                    """INSERT INTO kb.library_grant(library_id,principal_id,role)
                       VALUES(%s,%s,%s)""",
                    (world["lib"], world["principal"].principal_id, original_role),
                )

        with psycopg.connect(world["admin"]) as admin:
            admin.execute("DELETE FROM kb.fragment WHERE id=%s", (first_fragment_id,))
        with transaction_identity(conn, world["principal"]):
            _result(conn, valid, "actual_fragment_missing", False)


def test_manifest_read_is_bounded_and_set_based(world):
    source_ids = []
    source_entries = []

    with psycopg.connect(world["dsn"]) as conn:
        for index in range(10):
            text = (
                f"Quartzite {index} marks the first sentence.\n"
                f"Carnelian {index} marks the second sentence.\n"
                f"Zircon {index} marks the third sentence."
            )
            ident = parsed(world, text)
            source_ids.append(ident)
            snapshot = capture_snapshot(
                conn,
                world["principal"],
                [ident],
                f"Quartzite {index}",
            )
            entry = snapshot["manifest"]["sources"][0]
            assert entry["source_id"] == str(ident)
            source_entries.append(entry)

    with psycopg.connect(world["dsn"]) as conn:
        with transaction_identity(conn, world["principal"]):
            rows = conn.execute(
                """SELECT id,source_id,text
                   FROM kb.fragment
                   WHERE source_id=ANY(%s)
                   ORDER BY source_id,ordinal""",
                (source_ids,),
            ).fetchall()

    grouped = {source_id: [] for source_id in source_ids}
    for fragment_id, source_id, text in rows:
        assert source_id in grouped
        grouped[source_id].append((fragment_id, source_id, text))
    assert all(grouped.values())
    assert sum(len(items) for items in grouped.values()) >= 24

    # Reserve one fragment per source before filling to the legal fragment
    # limit, so source coverage remains part of the valid bounded case.
    selected = [grouped[source_id][0] for source_id in source_ids]
    for source_id in source_ids:
        for fragment in grouped[source_id][1:]:
            if len(selected) == 24:
                break
            selected.append(fragment)
        if len(selected) == 24:
            break
    assert len(selected) == 24

    manifest = {
        "sources": source_entries,
        "fragments": [
            {
                "fragment_id": str(fragment_id),
                "source_id": str(source_id),
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "excerpt_start": 0,
                "excerpt_end": len(text),
            }
            for fragment_id, source_id, text in selected
        ],
    }
    assert len(manifest["sources"]) == 10
    assert len(manifest["fragments"]) == 24

    with psycopg.connect(world["dsn"]) as conn:
        _install_reference(conn)
        with transaction_identity(conn, world["principal"]):
            _result(conn, manifest, "legal_10_source_24_fragment_bound", True)

            explain_value = conn.execute(
                """EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON)
                   SELECT kb.can_read_manifest(%s)""",
                (Jsonb(manifest),),
            ).fetchone()[0]
            if isinstance(explain_value, str):
                explain_value = json.loads(explain_value)
            plan = explain_value[0]["Plan"]
            assert plan["Actual Rows"] == 1

            _prosrc, volatility, security_definer, config = conn.execute(
                """SELECT prosrc,provolatile,prosecdef,proconfig
                   FROM pg_proc
                   WHERE oid='kb.can_read_manifest(jsonb)'::regprocedure"""
            ).fetchone()
            assert volatility == "s"
            assert security_definer is False
            assert any(
                setting.replace(" ", "") == "search_path=pg_catalog,kb" for setting in config
            )
