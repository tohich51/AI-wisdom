from __future__ import annotations

from typing import Any

import psycopg

from kb.access.policy import Principal


def check_admission(cur: Any, principal: Principal) -> None:
    try:
        cur.execute(
            "SELECT kb.check_processing_admission(%s,%s)",
            (principal.principal_id, principal.account_id),
        )
    except psycopg.errors.CheckViolation as exc:
        if exc.sqlstate == "23514" and exc.diag.message_primary == "processing_queue_full":
            raise ValueError("processing_queue_full") from None
        raise
