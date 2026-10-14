"""One durable Research task on the existing serial ingestion worker."""

import os
from uuid import UUID, uuid4

import httpx
import procrastinate
import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog.grounding import check_snapshot, resolve_citation
from kb.catalog.research import checked_run
from kb.model_research import Draft


class AttemptInProgress(RuntimeError):
    """Redelivery waits until the previous bounded attempt lease has expired."""


def register_research_task(app, dsn):
    @app.task(
        name="kb.research",
        queue="ingestion",
        retry=procrastinate.RetryStrategy(max_attempts=6, wait=480),
    )
    def process(run_id: str, principal_id: str, account_id: str):
        run_research(
            dsn,
            UUID(run_id),
            Principal(UUID(principal_id), UUID(account_id)),
            os.environ.get("KB_MODEL_RUNNER_URL"),
            os.environ.get("KB_WORKER_TOKEN"),
        )

    return process


def run_research(dsn, run_id, principal, runner_url, worker_token):
    attempt = uuid4()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        try:
            with transaction_identity(conn, principal):
                row = checked_run(conn.cursor(row_factory=dict_row), principal, run_id)
                if row["status"] in ("done", "failed"):
                    return
                allowed = conn.execute(
                    "SELECT kb.research_generation_allowed(%s,%s,%s) AS allowed",
                    (principal.account_id, row["library_id"], Jsonb(row["manifest"])),
                ).fetchone()
                if allowed["allowed"] is not True:
                    raise AccessDenied("forbidden")
                claimed = conn.execute(
                    """UPDATE kb.research_run SET status='running',attempt_token=%s,
                      started_at=now(),error_code=NULL WHERE id=%s AND
                      (status='queued' OR (status='running' AND
                       started_at<now()-interval '35 minutes'))
                      RETURNING id""",
                    (attempt, run_id),
                ).fetchone()
                if claimed is None:
                    raise AttemptInProgress("attempt_in_progress")
                fragments = []
                for pin in row["manifest"]["fragments"]:
                    frag = conn.execute(
                        "SELECT text FROM kb.fragment WHERE id=%s", (pin["fragment_id"],)
                    ).fetchone()
                    fragments.append(
                        {
                            "id": pin["fragment_id"],
                            "source_id": pin["source_id"],
                            "text": frag["text"][pin["excerpt_start"] : pin["excerpt_end"]],
                        }
                    )
            # Identity-bound DB transaction is committed before provider I/O.
            if not runner_url or not worker_token:
                raise ValueError("model_unavailable")
            response = httpx.post(
                runner_url.rstrip("/") + "/research",
                json={
                    "task_id": str(run_id),
                    "kind": row["kind"],
                    "query": row["query"],
                    "fragments": fragments,
                },
                headers={"X-KB-Worker-Token": worker_token},
                timeout=1900,
            )
            response.raise_for_status()
            data = response.json()
            if (
                data.get("task_id") != str(run_id)
                or data.get("model") != "MiniMax-M3.1-Flash-Preview"
                or data.get("kind") != row["kind"]
                or type(data.get("support_verified")) is not bool
                or type(data.get("provider_calls")) is not int
                or data["provider_calls"] not in (1, 2)
            ):
                raise ValueError("invalid_model_output")
            draft = Draft.model_validate_json(
                __import__("json").dumps(
                    {key: data[key] for key in ("title", "no_evidence", "items", "relations")}
                )
            ).model_dump(mode="json")
            from kb.research_validation import validate_draft

            validate_draft(draft, fragments, row["kind"])
            if not draft["no_evidence"] and (
                not data["support_verified"] or data["provider_calls"] != 2
            ):
                raise ValueError("unverified_output")
            with transaction_identity(conn, principal), conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT id FROM kb.research_run WHERE id=%s AND status='running' "
                    "AND attempt_token=%s FOR UPDATE",
                    (run_id, attempt),
                )
                if cur.fetchone() is None:
                    raise AccessDenied("forbidden")
                current = checked_run(cur, principal, run_id)
                cur.execute(
                    "SELECT kb.research_generation_allowed(%s,%s,%s) AS allowed",
                    (principal.account_id, row["library_id"], Jsonb(row["manifest"])),
                )
                if cur.fetchone()["allowed"] is not True:
                    raise AccessDenied("forbidden")
                if current["status"] != "running" or current["attempt_token"] != attempt:
                    return
                check_snapshot(cur, row["manifest"])
                citations = {}
                for item in [*draft["items"], *draft["relations"]]:
                    refs = []
                    for quote in item.pop("citations"):
                        cite = resolve_citation(
                            cur, row["manifest"], UUID(quote["fragment_id"]), quote["quote"]
                        )
                        citations[cite["id"]] = cite
                        refs.append(cite["id"])
                    item["citation_ids"] = list(dict.fromkeys(refs))
                result = {
                    **draft,
                    "kind": row["kind"],
                    "citations": list(citations.values()),
                    "support_verified": data["support_verified"],
                    "provider_calls": data["provider_calls"],
                    "retrieval_mode": "selected_source_excerpts",
                    "context_limited": row["manifest"].get("context_limited", True),
                }
                if row["kind"] == "network" and not draft["no_evidence"]:
                    from kb.catalog.semantic import import_network

                    import_network(
                        cur, principal, row["library_id"], run_id, row["manifest"], result
                    )
                cur.execute(
                    """UPDATE kb.research_run SET status='done',result=%s,completed_at=now()
                      WHERE id=%s AND status='running' AND attempt_token=%s RETURNING id""",
                    (Jsonb(jsonable_encoder(result)), run_id, attempt),
                )
                if cur.fetchone() is None:
                    raise AccessDenied("forbidden")
        except AttemptInProgress:
            raise
        except AccessDenied:
            conn.rollback()
            with transaction_identity(conn, principal):
                conn.execute(
                    "SELECT kb.finish_revoked_research(%s,%s,%s)",
                    (run_id, principal.principal_id, principal.account_id),
                )
        except Exception:
            conn.rollback()
            with transaction_identity(conn, principal):
                conn.execute(
                    """UPDATE kb.research_run SET status='failed',error_code='generation_failed',
                      completed_at=now() WHERE id=%s AND status='running'
                      AND attempt_token=%s""",
                    (run_id, attempt),
                )
