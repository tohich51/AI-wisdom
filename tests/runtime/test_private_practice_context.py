"""A09 acceptance: one shared book is shared, two private cases stay private.

Real services exercised here: kb_app SQL (NOSUPERUSER/NOBYPASSRLS, RLS enforced), the
real libraries / policies / writes / runs / registry service modules, the real
semantic-search service with the real shared CandidateCache instance, and the real MCP
dispatcher (`build_mcp` + `server.call_tool`) reached through a synthetic bearer token.
Two ordinary read-only MCP `search_knowledge` calls by Bob return the shared book and
never any personal practice or metric of Alice (or of Bob), in retrieval, in the payload
the agent/model actually receives, in the returned payload, or in the cache.

Labelled doubles only (never presented as real):
  * OIDC token resolver -> `SimpleNamespace` identity fixture (no native OIDC),
  * vector reader        -> `ReaderDouble` returning `FrozenCandidate` rows (no embeddings),
  * research runner HTTP -> fake `httpx.post` returning a fixed research payload (no live
    model, no provider call, no provider-quality claim).  Only what must never cross the
    boundary is asserted; nothing about answer quality is claimed.

No product code changes, no new dependencies, no new stand: everything below is built from
the existing services, real SQL and the actual shared cache object.
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from mcp.server.mcpserver.exceptions import UnexpectedToolError
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_final_project_manifest import _published
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, transaction_identity
from kb.adapters.vector_reader import FrozenCandidate
from kb.catalog import libraries, policies, writes
from kb.jobs import research as research_jobs
from kb.jobs.research import run_research
from kb.mcp.server import build_mcp
from kb.research import runs
from kb.retrieval import semantic_registry as registry
from kb.retrieval.candidate_cache import CandidateCache
from kb.retrieval.embedding_profile import SELECTED_PROFILE
from kb.retrieval.semantic_search import SemanticSearch

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

METRIC_KEYS = (
    "expected_result",
    "observed_result",
    "rating_basis",
    "average_rating",
    "use_id",
    "outcome_id",
)


@pytest.mark.asyncio
async def test_a09_shared_book_shared_personal_practice_private(monkeypatch, runtime_db, world):
    """Alice and Bob each keep a private case on one shared rule; nothing private travels."""
    Alice, Bob, lib = world["principal"], world["other"], world["lib"]
    with psycopg.connect(world["dsn"]) as conn:
        title = "Shared public manual"
        quote = "State the audience before drafting."
        rule, (source_id, fragment_id, _blob_hash) = _published(world, lib, title)

        libraries.set_grant(
            conn, Alice, lib, libraries.SetGrant(principal_id=Bob.principal_id, role="reader")
        )
        policies.put_policy(
            conn,
            Alice,
            lib,
            policies.LibraryPolicyInput(
                external_processing_allowed=True,
                cloud_mcp_allowed=True,
                export_allowed=True,
                generation_allowed=True,
            ),
        )
        conn.commit()

        # --- each actor keeps their OWN private use/outcome of the same shared published rule
        cases = {}
        for tag, actor in (("alice", Alice), ("bob", Bob)):
            stamp = uuid.uuid4().hex
            expected, where = f"{tag}-expected-canary-{stamp}", f"{tag}-where-canary-{stamp}"
            observed, basis = f"{tag}-observed-canary-{stamp}", f"{tag}-basis-canary-{stamp}"
            use = writes.start_use(conn, actor, rule["id"], rule["version_no"], expected, where)
            writes.mark_applied(conn, actor, use["id"])
            outcome = writes.record_outcome(
                conn,
                actor,
                use["id"],
                observed_result=observed,
                rating=4,
                rating_basis=basis,
                achievement="met",
                assessor_kind="agent",
            )
            cases[tag] = {
                "expected": expected,
                "where": where,
                "observed": observed,
                "basis": basis,
                "use_id": str(use["id"]),
                "outcome_id": str(outcome["id"]),
            }
        conn.commit()

        for me, other in (("alice", "bob"), ("bob", "alice")):
            actor = Alice if me == "alice" else Bob
            seen = json.dumps(writes.list_uses(conn, actor), default=str)
            assert cases[me]["expected"] in seen, "own private case must be visible to its owner"
            assert cases[other]["expected"] not in seen, (
                "case of the other member must stay private"
            )

        secrets = [value for case in cases.values() for value in case.values()]

        def clean(text, label):
            for secret in secrets:
                assert secret not in text, f"{label} leaked private value {secret!r}"
            for key in METRIC_KEYS:
                assert key not in text, f"{label} leaked personal metric key {key!r}"

        # --- shared knowledge seeded as the library owner through the real app role
        with transaction_identity(conn, Alice):
            kid = conn.execute(
                "INSERT INTO kb.knowledge(library_id, statement, kind) "
                "VALUES (%s,%s,'assertion') RETURNING id",
                (lib, f"{title}. {quote}"),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id,fragment_id) "
                "VALUES(%s,%s,%s)",
                (kid, source_id, fragment_id),
            )
        writes.publish(conn, Alice, kid, knowledge=True)

        # --- synthetic READY index account (admin only: never used to answer visibility)
        read_identity, index_identity = "kb-svc-read-" + lib.hex, "kb-svc-index-" + lib.hex
        acl = {
            "inherit_from_parent": False,
            "entries": [
                {"principal": read_identity, "rights": ["read"]},
                {"principal": index_identity, "rights": ["index", "read"]},
            ],
        }
        admin = psycopg.connect(world["admin"], autocommit=True)
        admin.execute(
            "INSERT INTO kb.index_account(library_id, account_ref, read_identity, index_identity,"
            " root_path, dimension, embedding_profile, acl, state)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ready')",
            (
                lib,
                "kb-lib-" + lib.hex,
                read_identity,
                index_identity,
                "/index",
                SELECTED_PROFILE.dimension,
                SELECTED_PROFILE.name,
                Jsonb(acl),
            ),
        )
        admin.execute(
            "UPDATE kb.index_account SET profile_hash = %s WHERE library_id = %s",
            (SELECTED_PROFILE.profile_hash, lib),
        )
        admin.close()

        # --- real provisioner-side capture / checkpoint / activation, single in-process run
        pconn = psycopg.connect(
            make_conninfo(world["admin"], user="kb_provisioner"), autocommit=True
        )
        try:
            snapshot = registry.capture(pconn, Alice, lib)
            build = registry.begin(pconn, Alice, snapshot)
            for doc in build.snapshot.documents:
                registry.checkpoint(pconn, Alice, build, doc)
            registry.activate(pconn, Alice, build)
        finally:
            pconn.close()

        class ReaderDouble:
            """Labeled double: records calls, returns deterministic FrozenCandidate rows."""

            def __init__(self):
                self.calls = []

            def find(self, identity, root, query, limit, *, deadline=None):
                self.calls.append(
                    SimpleNamespace(identity=identity, root=root, query=query, limit=limit)
                )
                return [
                    FrozenCandidate(build.document_uri(doc), 0.8)
                    for doc in build.snapshot.documents
                ]

            def close(self):
                return None

        reader = ReaderDouble()
        service = SemanticSearch(reader, candidates=CandidateCache())

        monkeypatch.setenv("KB_MCP_RESOURCE_URL", "https://gateway.example/mcp")
        monkeypatch.setenv("KB_OIDC_ISSUER", "https://id.example/realms/kb")
        monkeypatch.setenv("KB_MCP_CLIENT_ID", "fixture")
        identity = SimpleNamespace(
            principal_id=Alice.principal_id,
            account_id=Alice.account_id,
            scopes=frozenset({"kb:read"}),
            client_id="fixture",
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                resolve_token=lambda token: identity,
                db_connection=lambda: psycopg.connect(world["dsn"]),
                semantic_search=service,
            )
        )
        server, _ = build_mcp(app, ["gateway.example"])
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(
                request=SimpleNamespace(headers={"authorization": "Bearer synthetic-fixture"})
            )
        )

        payloads = []

        async def search(who):
            identity.principal_id = who.principal_id
            result = await server.call_tool(
                "search_knowledge",
                {
                    "query": "shared manual",
                    "library_ids": [str(lib)],
                    "limit": 3,
                    "use_generation": False,
                },
                context=ctx,
            )
            assert not result.is_error
            payloads.append(result.model_dump_json())
            return result

        def refuse_generation_during_retrieval(*args, **kwargs):
            raise AssertionError("Read-only MCP retrieval attempted a generation HTTP call")

        monkeypatch.setattr(research_jobs.httpx, "post", refuse_generation_during_retrieval)

        await search(Alice)  # warm the cache for the owner
        await search(Bob)  # Bob's first query: separate principal -> real retrieval
        await search(Bob)  # Bob repeats the same query: must hit his own cache entry

        assert len(reader.calls) == 2, f"expected 2 reader calls, got {reader.calls}"
        assert [c.query for c in reader.calls] == ["shared manual"] * 2
        assert all(c.root == build.resource_root and c.limit == 9 for c in reader.calls)
        for payload in payloads:
            assert str(kid) in payload and str(source_id) in payload, (
                "shared knowledge must be found"
            )
            clean(payload, "mcp search payload")

        cache_repr = repr(service.candidates._entries)
        clean(cache_repr, "shared candidate cache")
        assert any(build.document_uri(doc) in cache_repr for doc in build.snapshot.documents)

        # retrieval must not generate: the tool refuses and no provider is reachable at all
        with pytest.raises(UnexpectedToolError):
            await server.call_tool(
                "search_knowledge",
                {
                    "query": "shared manual",
                    "library_ids": [str(lib)],
                    "limit": 3,
                    "use_generation": True,
                },
                context=ctx,
            )

        # an ordinary member may not start generation for someone else's book (owner-only)
        with pytest.raises(AccessDenied):
            runs.submit_research(
                conn, Bob, lib, [source_id], "research", "Explain shared manual", str(uuid.uuid4())
            )
        conn.rollback()

        # --- actual model-bound INPUT stage: capture the worker -> runner request twice
        captured = []

        def fake_post(url, **kwargs):
            payload = copy.deepcopy(kwargs["json"])
            captured.append(payload)
            clean(json.dumps(payload, default=str), "research model input")
            fragments = payload["fragments"]
            assert fragments, "runner input must carry the retrieved fragments"
            for frag in fragments:
                assert frag["source_id"] == str(source_id), "only shared book fragments in context"
            return httpx.Response(
                200,
                json={
                    "task_id": payload["task_id"],
                    "model": "MiniMax-M3.1-Flash-Preview",
                    "kind": "research",
                    "title": "Shared manual",
                    "no_evidence": False,
                    "items": [
                        {
                            "heading": "Shared manual",
                            "text": quote,
                            "source_id": None,
                            "parent_index": None,
                            "citations": [{"fragment_id": fragments[0]["id"], "quote": quote}],
                        }
                    ],
                    "relations": [],
                    "support_verified": True,
                    "provider_calls": 2,
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(research_jobs.httpx, "post", fake_post)
        assert captured == [], "no generation may have happened during read-only MCP search"

        run_ids = []
        for _ in range(2):
            submitted = runs.submit_research(
                conn,
                Alice,
                lib,
                [source_id],
                "research",
                "Explain shared manual",
                str(uuid.uuid4()),
            )
            before = len(captured)
            run_research(
                world["worker"],
                submitted["run_id"],
                Alice,
                "http://synthetic-runner.invalid",
                "s" * 32,
            )
            assert len(captured) == before + 1, "each run must reach the runner exactly once"
            used_fragment = captured[-1]["fragments"][0]["id"]
            conn.commit()
            final = runs.get_research(conn, Alice, submitted["run_id"])
            assert final["status"] == "done"
            saved = json.dumps(final["result"], default=str)
            assert quote in saved and used_fragment in saved, "shared answer must cite the book"
            clean(saved, "saved research result")
            run_ids.append(submitted["run_id"])

        assert len(captured) == 2 and len(set(run_ids)) == 2
