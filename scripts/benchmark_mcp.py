"""Finite C34 acceptance using the existing delegated OAuth/MCP profile.

MiniMax supplies collect_phases below; root wires the already verified SDK/auth route.
"""

import argparse
import asyncio
import datetime
import hashlib
import json
import logging
import math
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from uuid import UUID

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def decoded(result):
    if result.is_error:
        raise ValueError("mcp_tool_refused")
    if result.structured_content is not None:
        return result.structured_content
    return json.loads("".join(b.text for b in result.content if b.type == "text"))


async def measured_search(session, case, library_id, source_id):
    started = time.monotonic()
    try:
        data = decoded(
            await session.call_tool(
                "search_knowledge",
                {
                    "query": case["query"],
                    "library_ids": [library_id],
                    "source_ids": [source_id],
                    "limit": 3,
                    "use_generation": False,
                },
            )
        )
        hits = data["hits"]
        expected = any(
            str(p.get("source_id")) == source_id for hit in hits for p in hit.get("provenance", [])
        )
        cited = bool(hits) and all(
            hit.get("provenance") and str(hit.get("library_id")) == library_id for hit in hits
        )
        statements = " ".join(str(hit.get("statement", "")) for hit in hits).casefold()
        relevant = any(term.casefold() in statements for term in case["expected_terms"])
        return {
            "ok": True,
            "ms": round((time.monotonic() - started) * 1000, 3),
            "case_id": case["id"],
            "mode": data["mode"],
            "fallback_reason": data.get("fallback_reason"),
            "hits": len(hits),
            "expected_source_present": expected,
            "all_hits_cited_in_library": cited,
            "relevance_keyword_check": relevant,
            "quality_ok": expected and cited and relevant and data["mode"] == "semantic",
        }
    except Exception as error:
        return {
            "ok": False,
            "ms": round((time.monotonic() - started) * 1000, 3),
            "case_id": case["id"],
            "error_code": type(error).__name__,
        }


def summaries(phases):
    result = []
    for phase in phases:
        rows = phase["requests"]
        latency = sorted(row["ms"] for row in rows)
        result.append(
            {
                **phase,
                "count": len(rows),
                "successful": sum(bool(r["ok"]) for r in rows),
                "errors": sum(not r["ok"] for r in rows),
                "p50_ms": latency[max(0, math.ceil(len(latency) * 0.5) - 1)] if rows else None,
                "p95_ms": latency[max(0, math.ceil(len(latency) * 0.95) - 1)] if rows else None,
            }
        )
    return result


async def execute(args, report, persist_upload):
    # Reuse the installed validated profile reader; never copy its secret into receipts.
    sys.path.insert(0, args.client_dir)
    from cloud_agent_client import APP, load_config

    config = load_config(Path(args.config))
    async with httpx.AsyncClient(
        base_url=APP, timeout=30, trust_env=False, follow_redirects=False
    ) as http:
        response = await http.post(
            config["token_endpoint"],
            data={
                "grant_type": "client_credentials",
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "scope": "openid kb:read kb:contribute kb:experience kb:export",
            },
        )
        response.raise_for_status()
        http.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        response = await http.get("/api/v1/me")
        response.raise_for_status()
        assert all(response.json()[key] == config[key] for key in ("principal_id", "account_id"))
        response = await http.get("/readyz")
        response.raise_for_status()
        ready = response.json()
        assert ready["status"] == "ready" and ready["release"] == args.expected_release
        report.update(server_release=ready["release"], identity_verified=True)
        async with AsyncExitStack() as stack:
            sessions = []
            for _ in range(3):
                read, write = await stack.enter_async_context(
                    streamable_http_client(config["mcp_url"], http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                sessions.append(session)
            phases = await collect_phases(
                http,
                sessions,
                library_id=args.library,
                source_id=args.source,
                cases=report["dataset"],
                upload_key=args.run_id + "-upload",
                persist_upload=persist_upload,
                phases_out=report["phases"],
            )
            rows = [row for phase in phases for row in phase["requests"]]
            report["status"] = (
                "passed"
                if len(rows) == 20
                and all(row["ok"] and row.get("quality_ok", True) for row in rows)
                else "failed"
            )


def main():
    if not __debug__:
        raise RuntimeError("optimized execution unsupported")
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "config",
        "client-dir",
        "library",
        "source",
        "dataset",
        "run-id",
        "output",
        "expected-release",
    ):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    for value in (args.library, args.source, args.run_id):
        assert str(UUID(value)) == value
    output = Path(args.output)
    intent = output.with_name(output.name + ".intent.json")
    assert output.is_absolute() and output.parent.is_dir()
    assert not output.exists() and not intent.exists(), "Reconcile the existing exact run"
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    dataset_bytes = Path(args.dataset).read_bytes()
    if (
        hashlib.sha256(dataset_bytes).hexdigest()
        != "c5f124cca7d36ba0f49da73607a21514dec833251e21cf48cbb87ed1dab9429f"
    ):
        raise ValueError("unapproved benchmark dataset")
    cases = json.loads(dataset_bytes)
    assert len(cases) == 3 and all(c["query"] and c["expected_terms"] for c in cases)
    started = datetime.datetime.now(datetime.UTC).isoformat()
    state = {
        "run_id": args.run_id,
        "library": args.library,
        "source": args.source,
        "payload_sha256": hashlib.sha256(
            b"C34 synthetic upload. This is disposable benchmark input, not a real book."
        ).hexdigest(),
        "upload": "not_observed",
        "planned_searches": 19,
        "planned_uploads": 1,
        "started_at": started,
    }

    def save_intent():
        temporary = intent.with_name(intent.name + ".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        os.replace(temporary, intent)

    def persist_upload(payload):
        state["upload"] = payload
        save_intent()

    with intent.open("x") as initial:
        initial.write(json.dumps(state, indent=2) + "\n")
    report = {
        "run_id": args.run_id,
        "status": "failed",
        "started_at": started,
        "dataset": cases,
        "expected_source_id": args.source,
        "transport": (
            "actual public HTTPS + official SDK; three sessions of one existing delegated identity"
        ),
        "cold_definition": "fresh harness session; provider/OS/DB caches not flushed",
        "relevance_definition": (
            "fixed expected-source and keyword heuristic; not human relevance grading"
        ),
        "phases": [],
        "resource_metrics_not_in_client": True,
        "hour_load_run": False,
        "generation_requested": False,
        "upload_processing_requested": False,
    }
    try:
        asyncio.run(asyncio.wait_for(execute(args, report, persist_upload), timeout=180))
    except Exception as error:
        report.update(status="failed", error_code=type(error).__name__)
    finally:
        report["phases"] = summaries(report["phases"])
        report.update(
            finished_at=datetime.datetime.now(datetime.UTC).isoformat(), upload=state["upload"]
        )
        output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "upload_effect": "observed"
                if isinstance(state["upload"], dict)
                else "not_observed_or_not_started",
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


async def collect_phases(
    http, sessions, *, library_id, source_id, cases, upload_key, persist_upload, phases_out=None
):
    phases = phases_out if phases_out is not None else []
    current_phase = None

    def stamp():
        return datetime.datetime.now(datetime.UTC).isoformat()

    def elapsed_ms(started):
        return round((time.monotonic() - started) * 1000, 3)

    def record(row):
        current_phase["requests"].append(row)
        return row

    async def run_search(session, case):
        started = time.monotonic()
        try:
            row = dict(await measured_search(session, case, library_id, source_id))
            row.setdefault("ok", True)
            row.setdefault("ms", elapsed_ms(started))
            return record(row)
        except asyncio.CancelledError:
            record({"ok": False, "ms": elapsed_ms(started), "error_code": "cancelled"})
            raise
        except Exception as exc:
            return record(
                {"ok": False, "ms": elapsed_ms(started), "error_code": type(exc).__name__}
            )

    async def upload():
        started = time.monotonic()
        status = None
        try:
            response = await http.post(
                f"/api/v1/libraries/{library_id}/sources/upload",
                files={
                    "file": (
                        "c34-synthetic.txt",
                        (
                            b"C34 synthetic upload. This is disposable benchmark input, "
                            b"not a real book."
                        ),
                        "text/plain",
                    )
                },
                data={"title": "C34 synthetic upload", "idempotency_key": upload_key},
            )
            status = response.status_code
            if not 200 <= status < 300:
                return record(
                    {
                        "ok": False,
                        "ms": elapsed_ms(started),
                        "http_status": status,
                        "error_code": f"http_{status}",
                    }
                )
            result = response.json()
            payload = {
                "http_status": status,
                "source_id": result["source"]["id"],
                "status": result["status"],
            }
            persist_upload(payload)
            return record({"ok": True, "ms": elapsed_ms(started), **payload})
        except asyncio.CancelledError:
            record({"ok": False, "ms": elapsed_ms(started), "error_code": "cancelled"})
            raise
        except Exception as exc:
            row = {"ok": False, "ms": elapsed_ms(started), "error_code": type(exc).__name__}
            if status is not None:
                row["http_status"] = status
            return record(row)

    async def sequential(session):
        for case in cases:
            await run_search(session, case)

    async def three_clients_plus_upload():
        await asyncio.gather(
            run_search(sessions[0], cases[0]),
            run_search(sessions[1], cases[1]),
            run_search(sessions[2], cases[2]),
            upload(),
        )

    async def burst10():
        await asyncio.gather(*(run_search(sessions[i % 3], cases[i % 3]) for i in range(10)))

    async def run_phase(name, operation, planned):
        nonlocal current_phase
        phase = {
            "name": name,
            "started_at": stamp(),
            "finished_at": None,
            "planned_requests": planned,
            "requests": [],
        }
        phases.append(phase)
        current_phase = phase
        try:
            await asyncio.wait_for(operation(), timeout=90)
        except TimeoutError:
            phase["error_code"] = "phase_timeout"
        except asyncio.CancelledError:
            phase["error_code"] = "cancelled"
            raise
        except Exception as exc:
            phase["error_code"] = type(exc).__name__
        finally:
            phase["finished_at"] = stamp()

    await run_phase("cold_fresh_session", lambda: sequential(sessions[0]), 3)
    await run_phase("warm_same_sessions", lambda: sequential(sessions[0]), 3)
    await run_phase("three_clients_plus_upload", three_clients_plus_upload, 4)
    await run_phase("burst10", burst10, 10)
    return phases


if __name__ == "__main__":
    raise SystemExit(main())
