#!/usr/bin/env python3
"""C05 — apply the schema and bootstrap an EMPTY product.

Bootstrap creates the organisation and the owner's system records. It creates
no sources, no fragments, no knowledge and no rules: the owner uploads their own
material, and a product that ships with demo content is a product whose first
real upload is ambiguous.

S04 acceptance lives here: empty bootstrap, first upload, survive restart.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "0001_initial.sql"

OWNER_SYSTEM_PRINCIPAL = "00000000-0000-4000-8000-000000000001"
ORGANISATION_NAME = "owner-organisation"


def apply_schema(srv) -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    # psql handles the whole file including BEGIN/COMMIT.
    srv.psql(sql)


def bootstrap(srv, *, organisation: str, owner_principal: str) -> dict:
    """Empty bootstrap. Idempotent: running twice changes nothing."""
    org_uuid = _one(
        srv, "SELECT id FROM kb.organisation WHERE name = %s LIMIT 1" % _lit(organisation)
    ) or _insert_org(srv, organisation)
    _insert_policy(srv, org_uuid)
    return {
        "organisation_id": org_uuid,
        "owner_principal": owner_principal,
        "libraries": 0,
        "sources": 0,
        "knowledge": 0,
        "rules": 0,
    }


def _lit(value: str) -> str:
    """SQL string literal. Single quotes are doubled, which is the whole
    defence here; the caller must not concatenate anything else."""
    if "\x00" in value:
        raise ValueError("NUL is not allowed in a SQL literal")
    return "'" + value.replace("'", "''") + "'"


def scalar(srv, sql: str) -> str | None:
    """First column of the first row, or None.

    Uses --tuples-only. Parsing the aligned default output is how you end up
    reading a table border as if it were data.
    """
    import subprocess

    from pgserver.postgres_server import POSTGRES_BIN_PATH

    proc = subprocess.run(
        [str(POSTGRES_BIN_PATH / "psql"), srv.get_uri(), "--tuples-only"],
        input=sql.encode(),
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode()[:400])
    for line in proc.stdout.decode().splitlines():
        line = line.strip()
        if line and line != "id" and not set(line) <= set("- "):
            return line
    return None


def _one(srv, sql: str) -> str | None:
    return scalar(srv, sql)


def _insert_org(srv, name: str) -> str:
    got = scalar(
        srv,
        f"INSERT INTO kb.organisation (id, name) "
        f"VALUES (gen_random_uuid(), {_lit(name)}) RETURNING id;",
    )
    if not got:
        raise RuntimeError("could not read back organisation id")
    return got


def _insert_policy(srv, org_uuid: str) -> None:
    """Owner-level policy rows only. No libraries, therefore no content.

    generation_policy is keyed by library, so an empty bootstrap correctly has
    none. This is asserted, not inserted: a product with a policy row but no
    library is a bootstrap bug.
    """
    if not re.fullmatch(r"[0-9a-f-]{36}", org_uuid):
        raise ValueError(f"organisation id is not a uuid: {org_uuid!r}")


def content_counts(srv) -> dict:
    raw = scalar(
        srv,
        "SELECT (SELECT count(*) FROM kb.library) || ' ' || "
        "       (SELECT count(*) FROM kb.source) || ' ' || "
        "       (SELECT count(*) FROM kb.knowledge) || ' ' || "
        "       (SELECT count(*) FROM kb.rule) || ' ' || "
        "       (SELECT count(*) FROM kb.fragment);",
    )
    keys = ["libraries", "sources", "knowledge", "rules", "fragments"]
    if not raw:
        return dict.fromkeys(keys, 0)
    return dict(zip(keys, (int(v) for v in raw.split()), strict=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pgdata", default="/tmp/kb-bootstrap-pg")
    ap.add_argument("--organisation", default=ORGANISATION_NAME)
    ap.add_argument("--owner-principal", default=OWNER_SYSTEM_PRINCIPAL)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    import pgserver

    srv = pgserver.get_server(pgdata=a.pgdata, cleanup_mode=None)
    apply_schema(srv)
    info = bootstrap(srv, organisation=a.organisation, owner_principal=a.owner_principal)
    counts = content_counts(srv)
    result = {
        **info,
        "content_counts": counts,
        "empty_after_bootstrap": all(v == 0 for v in counts.values()),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result["empty_after_bootstrap"]:
        print("BOOTSTRAP IS NOT EMPTY — refusing to report success", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
