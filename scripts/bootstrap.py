#!/usr/bin/env python3
"""C05 — install the schema and bootstrap an EMPTY product.

Bootstrap creates the organisation and nothing else. No libraries, no sources,
no fragments, no knowledge, no rules and no grants: the owner uploads their own
material, and a product that ships with demo content is a product whose first
real upload is ambiguous.

S04 is a product requirement with two halves, and both have to hold at once:

* a clean, empty database reports zero content after bootstrap;
* a repeat run over a database that already holds the owner's uploads returns
  the real counts, destroys nothing, and reports empty_after_bootstrap=false.

Applying only 0001 satisfied neither. The later migrations were never applied,
so the "schema" was not the product's schema, and the counts printed afterwards
were literal zeros written by this script rather than anything read from the
database — a report that would happily call a populated product empty.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

from kb.migrate import apply_migrations

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"

OWNER_SYSTEM_PRINCIPAL = "00000000-0000-4000-8000-000000000001"
ORGANISATION_NAME = "owner-organisation"


def apply_schema(srv) -> list[str]:
    """Install the current complete schema, idempotently.

    kb.migrate.apply_migrations applies every pending 0*.sql under its checksum
    ledger and advisory session lock, so this is correct on a clean pgdata, on
    a database that only ever saw 0001, and on one whose owner has already
    uploaded material: it brings the schema up to date and leaves the data
    alone. It is also the only thing here that touches the schema — no ad-hoc
    DDL, and no dropping, truncating or replacing a database.
    """
    return list(apply_migrations(srv.get_uri(), MIGRATIONS))


def bootstrap(srv, *, organisation: str, owner_principal: str) -> dict:
    """Create the organisation only. Idempotent: running again changes nothing.

    Nothing else is inserted: no sources, no fragments, no knowledge, no rules
    and no grants, so an empty start really is empty. owner_principal is
    operator metadata — who runs the product — and is only recorded here. The
    principal is not created, no issuer or user is invented for it, and it is
    granted nothing: identity and membership are bootstrapped elsewhere, and a
    fabricated identity row here would later be mistaken for a real owner.

    The counts are measured with content_counts rather than asserted, so on a
    database that already holds the owner's uploads they are the real counts.
    This function never deletes or rewrites anything to make them zero.
    """
    org_uuid = _one(
        srv, f"SELECT id FROM kb.organisation WHERE name = {_lit(organisation)} LIMIT 1;"
    ) or _insert_org(srv, organisation)
    if not re.fullmatch(r"[0-9a-f-]{36}", org_uuid):
        raise ValueError(f"organisation id is not a uuid: {org_uuid!r}")
    return {
        "organisation": organisation,
        "organisation_id": org_uuid,
        # Operator metadata only: recorded, never created, never granted.
        "owner_principal": owner_principal,
        # Flat mirrors of the measured counts, keeping the historic key names.
        **content_counts(srv),
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


def content_counts(srv) -> dict:
    """The real row counts, read from the tables the owner actually fills."""
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
        # An unreadable answer is not a zero. Defaulting to 0 here is exactly
        # how a report ends up calling a database that holds real data empty.
        raise RuntimeError("could not read content counts")
    return dict(zip(keys, (int(v) for v in raw.split()), strict=True))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pgdata", default="/tmp/kb-bootstrap-pg")
    ap.add_argument("--organisation", default=ORGANISATION_NAME)
    ap.add_argument("--owner-principal", default=OWNER_SYSTEM_PRINCIPAL)
    ap.add_argument(
        "--json",
        action="store_true",
        help="accepted for compatibility; the report is JSON either way",
    )
    a = ap.parse_args()

    import pgserver

    srv = pgserver.get_server(pgdata=a.pgdata, cleanup_mode=None)
    try:
        applied = apply_schema(srv)
    except Exception as error:
        # migrate deliberately keeps connection details out of its messages, so
        # neither does this.
        print(f"schema install failed ({type(error).__name__})", file=sys.stderr)
        return 1
    info = bootstrap(srv, organisation=a.organisation, owner_principal=a.owner_principal)
    counts = content_counts(srv)
    result = {
        **info,
        "content_counts": counts,
        "empty_after_bootstrap": all(v == 0 for v in counts.values()),
        "migrations_applied": applied,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not result["empty_after_bootstrap"]:
        # Not a failure, and nothing to clean up. A repeat run keeps the
        # owner's data and says so; refusing to report success is what turned
        # "preserve the upload" into "delete the upload or fail".
        print(
            "existing content kept: " + ", ".join(f"{k}={v}" for k, v in counts.items()),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
