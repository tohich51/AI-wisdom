"""C15 — the one-shot administrative entrypoint.

Run as a *rootless one-shot* (see ``deploy/maintenance/``), never as part of
the gateway and never from the ordinary worker queue. It holds the OpenViking
root key; the gateway does not, the parser does not, and the worker does not.
That separation is the card, and the only way it stays true is that this
program is the one place the key is even readable.

Three rules this program is built around:

* **No container runtime.** There is no ``subprocess`` import, no socket
  lookup and no shell-out anywhere in the package. A process that can start
  containers is a process that can be turned into the host, and this one is
  handed a root key. ``test_the_one_shot_cannot_reach_a_container_runtime``
  reads the source and fails if that ever changes.
* **No credentials in argv.** The database DSN and the OpenViking admin
  endpoint come from the environment. Anything passed as an argument is
  visible to every process on the host in ``ps``.
* **A missing dependency is a nonzero exit, never a green run.** When
  OpenViking is not configured this program exits 3 and says why. It does not
  print a success line, because the receipt for a provisioning run that did
  not happen is not a receipt.

In this tree there is no OpenViking admin client, and that is stated rather
than papered over: :func:`_admin_from_environment` refuses even when the
endpoint is set, because a flag is not an implementation. The boundary the
real client has to satisfy is
:class:`kb.retrieval.provisioning.IndexAdminPort`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Final
from uuid import UUID

import psycopg

from kb.access.policy import Principal
from kb.retrieval import provisioning
from kb.retrieval.provisioning import (
    IndexAdminPort,
    ProvisioningRefused,
    ProvisioningUnavailable,
    SecretSink,
    UnconfiguredIndexAdmin,
    UnconfiguredSecretSink,
)

#: Exit codes. 3 is reserved for "a required dependency is not here", which is
#: a failure and not a skip: this program is a provisioning run, and a
#: provisioning run that did not happen has not succeeded.
EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_REFUSED: Final = 4

DB_DSN_ENV: Final = "KB_DB_DSN"
ADMIN_URL_ENV: Final = "KB_OPENVIKING_ADMIN_URL"
ROOT_KEY_ENV: Final = "KB_OPENVIKING_ROOT_KEY"
SECRET_STORE_ENV: Final = "KB_SECRET_STORE_DIR"  # noqa: S105 - an env var NAME, not a value

#: Process roles that must not run the one-shot. The provisioning role is
#: deliberately not in this set: it is a NOSUPERUSER NOBYPASSRLS role with
#: five tables of grants. The DDL owner is, and a provisioning run holding
#: the DDL owner's powers plus a root key is a different and much larger
#: blast radius than this program needs.
FORBIDDEN_ROLES: Final = ("kb_schema_owner", "postgres")


def _admin_from_environment() -> IndexAdminPort:
    """The privileged boundary, or a refusal.

    Two refusals, in order, and both are real answers:

    * no endpoint configured — the environment has no OpenViking at all;
    * an endpoint configured but no client in this tree — the flag is set, the
      code is not, and reporting success here would be the exact lie this
      card is graded on.
    """
    if not os.environ.get(ADMIN_URL_ENV):
        raise ProvisioningUnavailable(UnconfiguredIndexAdmin.reason)
    raise ProvisioningUnavailable(
        f"{ADMIN_URL_ENV} is set, but this tree ships no OpenViking admin "
        "client. The boundary an implementation must satisfy is "
        "kb.retrieval.provisioning.IndexAdminPort; nothing in this card has "
        "ever called a real OpenViking server (docs/handoff/results/C15.json)."
    )


def _sink_from_environment() -> SecretSink:
    """The secret store, or a refusal.

    A key handed to an unwritable store is a key in a shell variable, and a
    key in a shell variable ends up in a history file.
    """
    if not os.environ.get(SECRET_STORE_ENV):
        raise ProvisioningUnavailable(
            f"{SECRET_STORE_ENV} is not set: refusing to hold a root key with "
            "nowhere to put the service keys it issues"
        )
    return UnconfiguredSecretSink()


def _connect() -> psycopg.Connection:
    dsn = os.environ.get(DB_DSN_ENV)
    if not dsn:
        raise SystemExit(f"{DB_DSN_ENV} is not set")
    conn = psycopg.connect(dsn, autocommit=True)
    role = conn.execute(
        "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if role is None:  # pragma: no cover - current_user always exists
        raise SystemExit("cannot determine the connected role")
    if role[0] in FORBIDDEN_ROLES or role[1]:
        raise SystemExit(
            f"refusing to run the one-shot as {role[0]}: it must run as the "
            "kb_provisioner role, which is not a superuser and does not "
            "bypass RLS"
        )
    return conn


def _principal_for(conn: psycopg.Connection, request_id: UUID) -> Principal:
    """Recover the identity of the request the one-shot is carrying out.

    The principal is *transported* from the application that filed the
    request, through a database row, and the RLS policies re-derive the
    caller's role from ``kb.library_grant`` rather than trusting the row. The
    one-shot therefore acts as somebody, with somebody's authority, and not as
    itself: holding the root key does not widen what the policies admit.
    """
    row = conn.execute(
        "SELECT requested_by FROM kb.provisioning_request WHERE id = %s", (request_id,)
    ).fetchone()
    if row is None:
        raise ProvisioningRefused("no such provisioning request")
    account = conn.execute("SELECT gen_random_uuid()").fetchone()
    if account is None:  # pragma: no cover - gen_random_uuid always returns a row
        raise SystemExit("cannot mint an account id")
    return Principal(principal_id=row[0], account_id=account[0], generation_watermark=1)


def _cmd_list_requests(conn: psycopg.Connection) -> int:
    """Print the open requests. Reads PostgreSQL only — no OpenViking needed.

    This subcommand is the one part of the one-shot that is genuinely runnable
    without the index tier, and it is the honest way to look at a stalled
    provisioning queue in an environment that has no OpenViking.

    The library's *name* is not printed. Fetching it would mean reading
    ``kb.library`` without a transport principal, and the RLS model answers
    that with an empty set — correctly, since a queue listing is not an
    identity. The request id and the library id are what the operator needs to
    act; ``show <library_id>`` resolves the rest once a principal is in play.
    """
    rows = conn.execute(
        """
        SELECT id, library_id, requested_by, state, attempt, requested_at
          FROM kb.provisioning_request
         WHERE state IN ('requested','running')
         ORDER BY requested_at, id
        """
    ).fetchall()
    if not rows:
        print("no open provisioning requests")
        return EXIT_OK
    for row in rows:
        print(
            json.dumps(
                {
                    "request_id": str(row[0]),
                    "library_id": str(row[1]),
                    "requested_by": str(row[2]),
                    "state": row[3],
                    "attempt": row[4],
                    "requested_at": row[5].isoformat(),
                },
                ensure_ascii=False,
            )
        )
    return EXIT_OK


def _cmd_run(conn: psycopg.Connection, request_id: UUID) -> int:
    """Carry out one request, or exit nonzero without touching anything."""
    principal = _principal_for(conn, request_id)
    admin = _admin_from_environment()
    sink = _sink_from_environment()
    outcome = provisioning.run_provisioning(conn, principal, request_id, admin, sink)
    # The outcome model has no field that could hold a key, so printing it is
    # safe by construction rather than by care.
    print(outcome.model_dump_json(indent=2))
    return EXIT_OK


def _cmd_show(conn: psycopg.Connection, library_id: UUID) -> int:
    """Print the account mapping for a library. No secrets, ever."""
    rows = conn.execute(
        """
        SELECT library_id, account_ref, read_identity, index_identity, root_path,
               dimension, embedding_profile, state, acl
          FROM kb.index_account
         WHERE library_id = %s
        """,
        (library_id,),
    ).fetchall()
    if not rows:
        print("no provisioned account for that library")
        return EXIT_OK
    for row in rows:
        print(
            json.dumps(
                {
                    "library_id": str(row[0]),
                    "account_ref": row[1],
                    "read_identity": row[2],
                    "index_identity": row[3],
                    "root_path": row[4],
                    "dimension": row[5],
                    "embedding_profile": row[6],
                    "state": row[7],
                    "acl": row[8],
                },
                ensure_ascii=False,
            )
        )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kb-provision-index",
        description=(
            "One-shot provisioning of OpenViking accounts for the retrieval "
            "tier. Holds the root key; run it as the kb_provisioner role, "
            "never as the gateway and never from the worker queue."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-requests", help="open provisioning requests (no OpenViking needed)")
    run_cmd = sub.add_parser("run", help="carry out one provisioning request")
    run_cmd.add_argument("request_id", type=UUID)
    show_cmd = sub.add_parser("show", help="print the account mapping for a library")
    show_cmd.add_argument("library_id", type=UUID)

    args = parser.parse_args(argv)
    try:
        conn = _connect()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    try:
        if args.command == "list-requests":
            return _cmd_list_requests(conn)
        if args.command == "show":
            return _cmd_show(conn, args.library_id)
        return _cmd_run(conn, args.request_id)
    except ProvisioningUnavailable as exc:
        print(f"CANNOT PROVISION: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ProvisioningRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except psycopg.Error as exc:
        print(f"DATABASE REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - covered by test_boundaries.py as a real process
    raise SystemExit(main())
