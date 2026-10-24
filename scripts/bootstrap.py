#!/usr/bin/env python3
"""Install the full schema and optionally bind an explicit first owner."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import uuid

import psycopg

from kb.jobs.schema import install_queue
from kb.migrate import apply_migrations

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"

OWNER_SYSTEM_PRINCIPAL = "00000000-0000-4000-8000-000000000001"
ORGANISATION_NAME = "owner-organisation"


class BootstrapError(RuntimeError):
    """Safe operator refusal without identity or connection values."""


class ExistingDatabase:
    """Connection adapter; never expose the DSN in a representation."""

    def __init__(self, dsn: str):
        self._dsn = dsn

    def get_uri(self) -> str:
        return self._dsn


def apply_schema(srv) -> list[str]:
    """Apply every pending checksummed migration without replacing data."""
    return list(apply_migrations(srv.get_uri(), MIGRATIONS))


def bootstrap(
    srv,
    *,
    organisation: str,
    owner_principal: str,
    owner_issuer: str | None = None,
    owner_subject: str | None = None,
) -> dict:
    """Keep content empty; optionally bind operator-verified identity coordinates."""
    issuer, subject = _present(owner_issuer), _present(owner_subject)
    if (issuer is None) != (subject is None):
        # Before any write: a half identity must not reach the database.
        raise BootstrapError("owner_issuer and owner_subject must be given together")
    principal = _uuid(owner_principal, "owner principal") if issuer else owner_principal

    org_uuid = _one(
        srv,
        f"SELECT id FROM kb.organisation WHERE name = {_lit(organisation)} LIMIT 1;",
    ) or _insert_org(srv, organisation)
    try:
        uuid.UUID(str(org_uuid))
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"organisation id is not a uuid: {org_uuid!r}") from None

    owner = {"owner_configured": bool(issuer), "owner_setup_required": not issuer}
    if issuer:
        owner = setup_owner(
            srv,
            org_uuid,
            owner_principal=principal,
            owner_issuer=issuer,
            owner_subject=subject,
        )
    return {
        "organisation": organisation,
        "organisation_id": org_uuid,
        # Existing metadata key; explicit setup is reported separately.
        "owner_principal": principal if issuer else owner_principal,
        **owner,
        # Flat mirrors of the measured counts, keeping the historic key names.
        **content_counts(srv),
    }


def setup_owner(
    srv,
    organisation_id,
    *,
    owner_principal: str,
    owner_issuer: str | None = None,
    owner_subject: str | None = None,
) -> dict:
    """Bind the first owner atomically; never replace or reactivate a membership."""
    issuer, subject = _present(owner_issuer), _present(owner_subject)
    if (issuer is None) != (subject is None):
        raise BootstrapError("owner_issuer and owner_subject must be given together")
    if issuer is None:
        raise BootstrapError("an owner needs both issuer and subject")
    principal = _uuid(owner_principal, "owner principal")
    org = _uuid(organisation_id, "organisation id")

    with psycopg.connect(srv.get_uri()) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM kb.organisation WHERE id = %s FOR UPDATE;", (org,))
        if cur.fetchone() is None:
            raise BootstrapError("organisation does not exist")

        cur.execute(
            "SELECT principal_id FROM kb.organisation_admin WHERE organisation_id = %s;",
            (org,),
        )
        admins = {str(row[0]) for row in cur.fetchall()}
        if admins - {principal}:
            raise BootstrapError("organisation already has an administrator")

        cur.execute(
            "SELECT principal_id FROM kb.membership"
            " WHERE organisation_id = %s AND issuer = %s AND subject = %s;",
            (org, issuer, subject),
        )
        if {str(row[0]) for row in cur.fetchall()} - {principal}:
            raise BootstrapError("issuer/subject identify another principal here")

        cur.execute(
            "SELECT issuer, subject, status FROM kb.membership"
            " WHERE organisation_id = %s AND principal_id = %s;",
            (org, principal),
        )
        row = cur.fetchone()
        if row is None:
            try:
                cur.execute(
                    "INSERT INTO kb.membership"
                    " (organisation_id, principal_id, issuer, subject, status)"
                    " VALUES (%s, %s, %s, %s, 'active') RETURNING 1;",
                    (org, principal, issuer, subject),
                )
            except psycopg.errors.UniqueViolation:
                raise BootstrapError("membership changed under this bootstrap") from None
            membership = "created"
        else:
            if (row[0], row[1]) != (issuer, subject):
                raise BootstrapError("owner membership has different issuer/subject")
            if row[2] != "active":
                raise BootstrapError("owner membership is deactivated")
            membership = "existing"

        cur.execute(
            "INSERT INTO kb.organisation_admin (organisation_id, principal_id)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING;",
            (org, principal),
        )

    return {
        "owner_configured": True,
        "owner_setup_required": False,
        "owner_membership": membership,
        "owner_admin": "existing" if principal in admins else "granted",
    }


def _present(value) -> str | None:
    """The value, or None when it is absent or blank."""
    return value if value is not None and str(value).strip() else None


def _uuid(value, what: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise BootstrapError(f"{what} is not a uuid") from None


def _lit(value: str) -> str:
    """SQL string literal. Single quotes are doubled, which is the whole
    defence here; the caller must not concatenate anything else."""
    if "\x00" in value:
        raise ValueError("NUL is not allowed in a SQL literal")
    return "'" + value.replace("'", "''") + "'"


def scalar(srv, sql: str) -> str | None:
    """Read one scalar using psycopg, including committing INSERT RETURNING."""
    with psycopg.connect(srv.get_uri()) as conn:
        row = conn.execute(sql).fetchone()
    return str(row[0]) if row and row[0] is not None else None


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
    connection = ap.add_mutually_exclusive_group()
    connection.add_argument("--pgdata", default=None)
    connection.add_argument(
        "--dsn-env",
        metavar="NAME",
        help="environment variable containing an existing database administrator DSN",
    )
    ap.add_argument("--organisation", default=ORGANISATION_NAME)
    ap.add_argument("--owner-principal", default=OWNER_SYSTEM_PRINCIPAL)
    ap.add_argument(
        "--owner-issuer",
        default=None,
        help="issuer of the owner identity, operator-verified; needs --owner-subject",
    )
    ap.add_argument(
        "--owner-subject",
        default=None,
        help="subject of the owner identity; needs --owner-issuer",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="accepted for compatibility; the report is JSON either way",
    )
    a = ap.parse_args()

    issuer, subject = _present(a.owner_issuer), _present(a.owner_subject)
    if (issuer is None) != (subject is None):
        # Refused here: before pgdata is opened, before any migration or write.
        ap.error("--owner-issuer and --owner-subject must be given together")

    if issuer is not None:
        try:
            _uuid(a.owner_principal, "owner principal")
        except BootstrapError:
            ap.error("owner principal is not a uuid")
    if a.dsn_env is not None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", a.dsn_env):
            ap.error("--dsn-env must name an environment variable")
        if issuer is None:
            ap.error("--dsn-env requires both owner identity flags")
        dsn = os.environ.get(a.dsn_env)
        if not dsn or not dsn.strip():
            ap.error("database environment variable is missing or empty")
        srv = ExistingDatabase(dsn)
    else:
        import pgserver

        srv = pgserver.get_server(pgdata=a.pgdata or "/tmp/kb-bootstrap-pg", cleanup_mode=None)
    try:
        applied = apply_schema(srv)
        install_queue(srv.get_uri())
    except Exception as error:
        # migrate deliberately keeps connection details out of its messages, so
        # neither does this.
        print(f"schema install failed ({type(error).__name__})", file=sys.stderr)
        return 1
    try:
        info = bootstrap(
            srv,
            organisation=a.organisation,
            owner_principal=a.owner_principal,
            owner_issuer=issuer,
            owner_subject=subject,
        )
    except Exception as error:
        # Type only: no connection string, no issuer, no subject.
        print(f"bootstrap failed ({type(error).__name__})", file=sys.stderr)
        return 1
    counts = {key: info[key] for key in ("libraries", "sources", "knowledge", "rules", "fragments")}
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
