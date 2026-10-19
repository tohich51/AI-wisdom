"""Small, transaction-local idempotency support for create operations.

Receipts contain only a canonical request digest and the created target id. No
request payload is persisted. The advisory lock follows the existing
``transaction_identity``/``pg_advisory_xact_lock`` pattern used by experience
writes; receipt visibility itself remains an RLS decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from kb.access.policy import Principal

LIBRARY_CREATE = "library_create"
PROJECT_CREATE = "project_create"
RULE_CREATE = "rule_create"
SEMANTIC_CONCEPT_CREATE = "semantic_concept_create"
SEMANTIC_RELATION_CREATE = "semantic_relation_create"


@dataclass(frozen=True)
class OperationReceipt:
    request_hash: str
    target_id: UUID


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError("invalid_idempotency_key")
    return value


def hash_create_request(operation_type: str, payload: dict[str, Any]) -> str:
    """Hash a deterministic JSON representation of the semantic request."""
    document = {"operation_type": operation_type, "payload": payload}
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def lock_create_operation(
    cur: psycopg.Cursor[Any],
    principal: Principal,
    operation_type: str,
    idempotency_key: str,
) -> None:
    """Serialize one principal/type/key until the surrounding transaction ends."""
    cur.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"create:{operation_type}:{principal.principal_id}:{idempotency_key}",),
    )


def find_create_operation(
    cur: psycopg.Cursor[Any],
    principal: Principal,
    operation_type: str,
    idempotency_key: str,
) -> OperationReceipt | None:
    """Read an owned receipt; the operation type and principal are explicit too."""
    cur.execute(
        """SELECT request_hash, target_id
           FROM kb.operation_receipt
           WHERE principal_id=%s AND operation_type=%s AND idempotency_key=%s""",
        (principal.principal_id, operation_type, idempotency_key),
    )
    row = cur.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return OperationReceipt(str(row["request_hash"]), row["target_id"])
    return OperationReceipt(str(row[0]), row[1])


def record_create_operation(
    cur: psycopg.Cursor[Any],
    principal: Principal,
    operation_type: str,
    idempotency_key: str,
    request_hash: str,
    target_id: UUID,
) -> None:
    """Insert the receipt inside the entity's existing transaction."""
    cur.execute(
        """INSERT INTO kb.operation_receipt
             (principal_id,operation_type,idempotency_key,request_hash,target_id)
           VALUES(%s,%s,%s,%s,%s)""",
        (principal.principal_id, operation_type, idempotency_key, request_hash, target_id),
    )
