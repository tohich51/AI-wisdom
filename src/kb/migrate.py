"""Apply ordered, checksummed PostgreSQL migrations."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg
from psycopg import ClientCursor

_LOCK_SQL = "SELECT pg_catalog.pg_advisory_lock(647231)"
_UNLOCK_SQL = "SELECT pg_catalog.pg_advisory_unlock(647231)"

_CREATE_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS public.pkb_schema_migration (
    filename text PRIMARY KEY,
    sha256 text NOT NULL,
    applied_at timestamp DEFAULT now()
);
"""
_REVOKE_LEDGER_SQL = """
REVOKE ALL ON TABLE public.pkb_schema_migration FROM PUBLIC;
"""
_READ_LEDGER_SQL = """
SELECT filename, sha256
FROM public.pkb_schema_migration
"""
_INSERT_LEDGER_SQL = """
INSERT INTO public.pkb_schema_migration (filename, sha256)
VALUES (%s, %s)
"""

_LINE_BREAK = r"(?:\r\n|\r|\n)"
_OUTER_BEGIN_RE = re.compile(
    rf"\A"
    rf"(?:[ \t]*{_LINE_BREAK}|[ \t]*--[^\r\n]*{_LINE_BREAK})*"
    rf"(?P<statement>BEGIN[ \t]*;[ \t]*"
    rf"(?:--[^\r\n]*(?:{_LINE_BREAK}|\Z)|{_LINE_BREAK}|\Z))",
    re.IGNORECASE,
)
_OUTER_COMMIT_RE = re.compile(
    rf"(?:\A|{_LINE_BREAK})[ \t]*"
    rf"(?P<statement>COMMIT[ \t]*;[ \t]*)"
    rf"(?:{_LINE_BREAK}|[ \t]|--[^\r\n]*(?:{_LINE_BREAK}|\Z))*\Z",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    """A migration error that is safe to display without connection details."""


def _strip_outer_transaction(sql: str) -> str:
    """Remove only a leading standalone BEGIN and a trailing standalone COMMIT."""
    begin = _OUTER_BEGIN_RE.match(sql)
    if begin is not None:
        sql = sql[begin.end("statement") :]

    commit = _OUTER_COMMIT_RE.search(sql)
    if commit is not None:
        sql = sql[: commit.start("statement")]

    return sql


def _ensure_ledger(connection: psycopg.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(_CREATE_LEDGER_SQL)
        cursor.execute(_REVOKE_LEDGER_SQL)


def _recorded_hashes(connection: psycopg.Connection) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(_READ_LEDGER_SQL)
        return {row[0]: row[1] for row in cursor.fetchall()}


def _pending_migrations(
    directory: Path, recorded_hashes: dict[str, str]
) -> list[tuple[str, str, str]]:
    paths = sorted(directory.glob("0*.sql"), key=lambda path: path.name)
    if not directory.is_dir() or not paths:
        raise MigrationError("migration directory is missing or empty")
    if set(recorded_hashes) - {path.name for path in paths}:
        raise MigrationError("an applied migration is missing from the source")
    pending: list[tuple[str, str, str]] = []

    for path in paths:
        filename = path.name
        try:
            payload = path.read_bytes()
        except OSError:
            raise MigrationError(f"{filename}: migration file cannot be read") from None

        checksum = hashlib.sha256(payload).hexdigest()
        if filename in recorded_hashes:
            if recorded_hashes[filename] != checksum:
                raise MigrationError(f"{filename}: checksum does not match the applied migration")
            continue

        try:
            sql = payload.decode("utf-8")
        except UnicodeError:
            raise MigrationError(f"{filename}: migration is not valid UTF-8") from None

        pending.append((filename, checksum, _strip_outer_transaction(sql)))

    return pending


def apply_migrations(dsn: str, directory: Path) -> list[str]:
    """Apply pending SQL files under an exclusive advisory session lock."""
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            lock_acquired = False
            try:
                with connection.cursor() as cursor:
                    cursor.execute(_LOCK_SQL)
                lock_acquired = True

                _ensure_ledger(connection)
                recorded_hashes = _recorded_hashes(connection)
                pending = _pending_migrations(directory, recorded_hashes)

                applied: list[str] = []
                for filename, checksum, sql in pending:
                    try:
                        with connection.transaction():
                            with ClientCursor(connection) as cursor:
                                cursor.execute(sql)
                            with connection.cursor() as cursor:
                                cursor.execute(_INSERT_LEDGER_SQL, (filename, checksum))
                    except Exception:
                        raise MigrationError(f"{filename}: migration transaction failed") from None

                    applied.append(filename)

                return applied
            finally:
                if lock_acquired:
                    body_failed = sys.exc_info()[0] is not None
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute(_UNLOCK_SQL)
                    except Exception:
                        if not body_failed:
                            raise
    except MigrationError:
        raise
    except Exception:
        raise MigrationError("migration runner failed") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(os.environ.get("KB_MIGRATIONS_DIR") or "/app/migrations"),
    )
    args = parser.parse_args()

    dsn = os.environ.get("KB_ADMIN_DATABASE_URL")
    if not dsn:
        parser.error("KB_ADMIN_DATABASE_URL is required")

    try:
        applied = apply_migrations(dsn, args.directory)
    except MigrationError as error:
        print(error, file=sys.stderr)
        return 1
    except Exception:
        print("migration failed", file=sys.stderr)
        return 1

    for filename in applied:
        print(filename)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
