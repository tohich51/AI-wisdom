"""Administrative OpenViking provisioning entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

import psycopg

from kb.access.policy import Principal
from kb.adapters.secrets import FileSecretSink, _reject_symlinks
from kb.retrieval import provisioning
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.openviking_admin import OpenVikingAdmin
from kb.retrieval.provisioning import (
    ProvisioningRefused,
    ProvisioningUnavailable,
)

EXIT_OK: Final = 0
EXIT_USAGE: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_REFUSED: Final = 4

DB_DSN_ENV: Final = "KB_DB_DSN"
INDEXER_DATABASE_URL_ENV: Final = "KB_INDEXER_DATABASE_URL"
ADMIN_URL_ENV: Final = "KB_OPENVIKING_URL"
LEGACY_ADMIN_URL_ENV: Final = "KB_OPENVIKING_ADMIN_URL"
ROOT_KEY_FILE_ENV: Final = "KB_OPENVIKING_ROOT_KEY_FILE"
KEYS_ROOT_ENV: Final = "KB_INDEX_KEYS_ROOT"
LEGACY_KEYS_ROOT_ENV: Final = "KB_SECRET_STORE_DIR"
BOOTSTRAP_KEYS_ROOT_ENV: Final = "KB_BOOTSTRAP_KEYS_ROOT"

PROVISIONER_ROLE: Final = "kb_provisioner"
WRITER_LOCK_KEY: Final = 112187
WRITER_LOCK_NAMESPACE: Final = 1
PENDING_LIMIT: Final = 25


@dataclass(frozen=True)
class _RuntimeConfig:
    url: str
    root_key_file: Path
    keys_root: Path
    bootstrap_root: Path


def _env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value
    return None


def _private_path(raw: str, *, directory: bool) -> Path:
    try:
        candidate = Path(raw).expanduser().absolute()
        _reject_symlinks(candidate)
        path = candidate.resolve(strict=True)
        _reject_symlinks(path)
        info = path.lstat()
    except (OSError, RuntimeError, ValueError):
        raise ProvisioningUnavailable("runtime_path_unavailable") from None

    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise ProvisioningUnavailable("runtime_path_not_private")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ProvisioningUnavailable("runtime_path_not_directory")
    if not directory:
        if not stat.S_ISREG(info.st_mode):
            raise ProvisioningUnavailable("runtime_root_key_not_regular")
        if info.st_size <= 0:
            raise ProvisioningUnavailable("runtime_root_key_empty")
    return path


def _runtime_config() -> _RuntimeConfig:
    url = _env(ADMIN_URL_ENV, LEGACY_ADMIN_URL_ENV)
    root_key_file = _env(ROOT_KEY_FILE_ENV)
    keys_root = _env(KEYS_ROOT_ENV, LEGACY_KEYS_ROOT_ENV)
    bootstrap_root = _env(BOOTSTRAP_KEYS_ROOT_ENV)
    if not all((url, root_key_file, keys_root, bootstrap_root)):
        raise ProvisioningUnavailable("runtime_configuration_unavailable")
    assert url and root_key_file and keys_root and bootstrap_root
    return _RuntimeConfig(
        url=url,
        root_key_file=_private_path(root_key_file, directory=False),
        keys_root=_private_path(keys_root, directory=True),
        bootstrap_root=_private_path(bootstrap_root, directory=True),
    )


def _close_quietly(conn: psycopg.Connection | None) -> bool:
    if conn is None:
        return True
    try:
        conn.close()
        return True
    except Exception:
        print("DATABASE REFUSED: connection_cleanup_failed", file=sys.stderr)
        return False


def _connect() -> psycopg.Connection:
    dsn = _env(DB_DSN_ENV, INDEXER_DATABASE_URL_ENV)
    if dsn is None:
        raise ProvisioningRefused("database_configuration_unavailable")

    conn: psycopg.Connection | None = None
    try:
        conn = psycopg.connect(dsn, autocommit=True)
        role = conn.execute(
            """
            SELECT session_user, current_user, rolsuper, rolbypassrls
              FROM pg_roles
             WHERE rolname = current_user
            """
        ).fetchone()
    except Exception:
        _close_quietly(conn)
        raise ProvisioningRefused("database_connection_refused") from None

    if (
        role is None
        or str(role[0]) != PROVISIONER_ROLE
        or str(role[1]) != PROVISIONER_ROLE
        or bool(role[2])
        or bool(role[3])
    ):
        _close_quietly(conn)
        raise ProvisioningRefused("forbidden_database_role")
    return conn


def _principal_for(conn: psycopg.Connection, request_id: UUID) -> Principal:
    row = conn.execute(
        """
        SELECT request_id, library_id, principal_id, account_id
          FROM kb.provisioning_context(%s, 1)
        """,
        (request_id,),
    ).fetchone()
    if row is None or row[2] is None or row[3] is None:
        raise ProvisioningRefused("provisioning_context_rejected")
    return Principal(
        principal_id=row[2],
        account_id=row[3],
        generation_watermark=1,
    )


def _pending_request_ids(conn: psycopg.Connection) -> list[UUID]:
    rows = conn.execute("SELECT request_id FROM kb.provisioning_context(NULL, 25)").fetchall()
    return [row[0] for row in rows[:PENDING_LIMIT]]


def _cmd_list_requests(conn: psycopg.Connection) -> int:
    rows = conn.execute(
        """
        SELECT id, library_id, requested_by, state, attempt, requested_at
          FROM kb.provisioning_request
         WHERE state IN ('requested', 'running')
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


def _merge_status(current: int, failure: int) -> int:
    if EXIT_UNAVAILABLE in (current, failure):
        return EXIT_UNAVAILABLE
    if EXIT_REFUSED in (current, failure):
        return EXIT_REFUSED
    return EXIT_OK


def _cmd_run(
    conn: psycopg.Connection,
    request_id: UUID | None = None,
) -> int:
    if request_id is None:
        request_ids = _pending_request_ids(conn)
    else:
        _principal_for(conn, request_id)
        request_ids = [request_id]

    config = _runtime_config()
    if not request_ids:
        print("no pending provisioning requests")
        return EXIT_OK

    admin: OpenVikingAdmin | None = None
    lock_held = False
    cleanup_failed = False
    status = EXIT_OK

    try:
        acquired = conn.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            (WRITER_LOCK_KEY, WRITER_LOCK_NAMESPACE),
        ).fetchone()
        if acquired is None or not acquired[0]:
            print("CANNOT PROVISION: writer_lock_busy", file=sys.stderr)
            status = EXIT_UNAVAILABLE
        else:
            lock_held = True
            sink = FileSecretSink(config.keys_root)
            admin = OpenVikingAdmin(
                config.url,
                config.root_key_file,
                config.bootstrap_root,
            )
            for pending_request_id in request_ids:
                try:
                    principal = _principal_for(conn, pending_request_id)
                    outcome = provisioning.run_provisioning(
                        conn,
                        principal,
                        pending_request_id,
                        admin,
                        sink,
                        dimension=SELECTED_PROFILE.dimension,
                        embedding_profile=SELECTED_PROFILE.name,
                    )
                    print(outcome.model_dump_json(indent=2))
                except ProvisioningRefused:
                    print("REFUSED: provisioning_refused", file=sys.stderr)
                    status = _merge_status(status, EXIT_REFUSED)
                except ProvisioningUnavailable:
                    print(
                        "CANNOT PROVISION: provisioning_unavailable",
                        file=sys.stderr,
                    )
                    status = _merge_status(status, EXIT_UNAVAILABLE)
                except psycopg.Error:
                    print("DATABASE REFUSED: database_error", file=sys.stderr)
                    status = _merge_status(status, EXIT_REFUSED)
                except Exception:
                    print("CANNOT PROVISION: internal_error", file=sys.stderr)
                    status = _merge_status(status, EXIT_UNAVAILABLE)
    finally:
        if admin is not None:
            try:
                admin.close()
            except Exception:
                print("CANNOT PROVISION: admin_cleanup_failed", file=sys.stderr)
                cleanup_failed = True
        if lock_held:
            try:
                unlocked = conn.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    (WRITER_LOCK_KEY, WRITER_LOCK_NAMESPACE),
                ).fetchone()
                if unlocked is None or not unlocked[0]:
                    raise RuntimeError
            except Exception:
                print(
                    "CANNOT PROVISION: writer_lock_release_failed",
                    file=sys.stderr,
                )
                cleanup_failed = True

    if cleanup_failed:
        status = _merge_status(status, EXIT_UNAVAILABLE)
    return status


def _cmd_show(conn: psycopg.Connection, library_id: UUID) -> int:
    candidates = conn.execute(
        """
        SELECT id
          FROM kb.provisioning_request
         WHERE library_id = %s
         ORDER BY requested_at DESC, id DESC
        """,
        (library_id,),
    ).fetchall()
    if not candidates:
        print("no provisioned account for that library")
        return EXIT_OK

    principal: Principal | None = None
    for candidate in candidates:
        try:
            principal = _principal_for(conn, candidate[0])
            break
        except ProvisioningRefused:
            continue
    if principal is None:
        raise ProvisioningRefused("provisioning_context_rejected")

    account = provisioning.load_account(conn, principal, library_id)
    if account is None:
        print("no provisioned account for that library")
        return EXIT_OK
    print(account.model_dump_json())
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb-provision-index")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list-requests")
    run_cmd = commands.add_parser("run")
    run_cmd.add_argument("request_id", type=UUID, nargs="?", metavar="REQUEST_ID")
    show_cmd = commands.add_parser("show")
    show_cmd.add_argument("library_id", type=UUID)

    args = parser.parse_args(argv)
    conn: psycopg.Connection | None = None
    status = EXIT_OK
    try:
        conn = _connect()
        if args.command == "list-requests":
            status = _cmd_list_requests(conn)
        elif args.command == "show":
            status = _cmd_show(conn, args.library_id)
        else:
            status = _cmd_run(conn, args.request_id)
    except ProvisioningUnavailable:
        print("CANNOT PROVISION: provisioning_unavailable", file=sys.stderr)
        status = EXIT_UNAVAILABLE
    except ProvisioningRefused:
        print("REFUSED: provisioning_refused", file=sys.stderr)
        status = EXIT_REFUSED
    except psycopg.Error:
        print("DATABASE REFUSED: database_error", file=sys.stderr)
        status = EXIT_REFUSED
    except Exception:
        print("CANNOT PROVISION: internal_error", file=sys.stderr)
        status = EXIT_UNAVAILABLE
    finally:
        if not _close_quietly(conn):
            status = EXIT_UNAVAILABLE
    return status


if __name__ == "__main__":
    raise SystemExit(main())
