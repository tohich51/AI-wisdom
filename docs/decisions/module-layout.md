# Module layout, /api/v1 and the operation registry

Status: accepted by the module F card `architecture-20261015-v1`.
Contract revision: `architecture-20261015-v1` (`kb.contracts.operations.CONTRACT_REVISION`).

## Why the layout changed

The package had grown two conventions at once. Everything a request could reach
lived in `http/`, `mcp/`, `catalog/`, `retrieval/` and `access/`, and then eight
more modules sat flat in `src/kb/`, one of which — `research_worker.py` — was a
durable queue consumer and another of which — `model_runner.py` — spawned
somebody else's CLI. A reader could not tell from the path whether a module
answered a request, wrote to the database, or waited on a queue, and the only
way to find out was to read it.

Three packages now carry that distinction, and they are the only ones the move
introduced.

| Package | Holds | Rule that decides membership |
| --- | --- | --- |
| `kb.jobs` | the queue schema, ingestion, the indexer, the research worker, the model runner | survives a restart: claims a task and persists the result |
| `kb.research` | the research decision — runs, grounding, notes, rule handoff, the semantic network, generation, references, validation | what a research run concluded, independent of who ran it |
| `kb.adapters` | the blob store, the OpenViking vector reader and writer, the secret store | reads or writes something outside the database that we do not own |

`kb.contracts`, `kb.access`, `kb.http`, `kb.mcp`, `kb.domain` and `kb.static` are
unchanged in membership. `kb.catalog` and `kb.retrieval` lost members to the three
packages above and gained none: the catalog is still the store and the retrieval
layer is still the semantic index, and neither is a job, a research conclusion or
a system we do not own.

None of the three new packages re-exports anything from its `__init__`. A
package root that pulled in its modules would make every consumer of a single
name pay for the whole layer, and would make an import cycle possible between a
worker and the code it serves.

## The completed move map

| Before | After | Non-import lines changed |
| --- | --- | --- |
| `src/kb/catalog/research.py` | `src/kb/research/runs.py` | 0 |
| `src/kb/catalog/notes.py` | `src/kb/research/notes.py` | 0 |
| `src/kb/catalog/grounding.py` | `src/kb/research/grounding.py` | 0 |
| `src/kb/catalog/research_rules.py` | `src/kb/research/rule_handoff.py` | 0 |
| `src/kb/catalog/semantic.py` | `src/kb/research/semantic.py` | 0 |
| `src/kb/model_research.py` | `src/kb/research/generation.py` | 0 |
| `src/kb/research_references.py` | `src/kb/research/references.py` | 0 |
| `src/kb/research_validation.py` | `src/kb/research/validation.py` | 0 |
| `src/kb/ingestion.py` | `src/kb/jobs/ingestion.py` | 0 |
| `src/kb/research_worker.py` | `src/kb/jobs/research.py` | 0 |
| `src/kb/indexer.py` | `src/kb/jobs/indexer.py` | 0 |
| `src/kb/model_runner.py` | `src/kb/jobs/model_runner.py` | 0 |
| `src/kb/queue_schema.py` | `src/kb/jobs/schema.py` | 0 |
| `src/kb/catalog/storage.py` | `src/kb/adapters/originals.py` | 0 |
| `src/kb/retrieval/vector_reader.py` | `src/kb/adapters/vector_reader.py` | 0 |
| `src/kb/retrieval/vector_writer.py` | `src/kb/adapters/vector_writer.py` | 0 |
| `src/kb/retrieval/secret_store.py` | `src/kb/adapters/secrets.py` | 0 |

All 17 assignments in the frozen `move-map.json` are applied. The move is a
rename plus import rewrites: the third column is the number of `+`/`-` lines
that `git diff -M -U0 31dbfdd1` reports for the file once imports, docstrings and
comments are excluded, so a non-zero value there is a change to behaviour and not
a change to a path.

The previous candidate moved 8 of the 17 to a destination the frozen map does not
name, and left 9 of them unmoved. `kb.model_runner` went to `kb.adapters`, which
contradicts the contract's own layer rule — `jobs` is what survives a restart, and
the model runner is a queue service behind the `model-runner` CLI name. It is in
`kb.jobs`. `kb.queue_schema` became `kb.jobs.queue`, `kb.model_research` became
`kb.research.model` and `kb.research_worker` kept its old basename inside
`kb.jobs`; the map names `kb.jobs.schema`, `kb.research.generation` and
`kb.jobs.research`.

Two stale-path forms had to be closed, and a dotted rewrite closes only one:

* `kb.catalog.research` and friends — rewritten everywhere in the lease.
* `from kb.catalog import notes, research, research_rules, semantic` — invisible
  to a dotted rewrite. It resolves through the package, so it keeps working if a
  module of the same name ever reappears in `kb.catalog`. Each such import was
  split, and the local names `research` and `research_rules` became `runs` and
  `rule_handoff` so no code still reads as if the module had not moved.

Every consumer is inside the lease: `src/kb`, `tests/`, `scripts/`,
`docs/decisions/`, `docs/runbooks/`, `spikes/`, `justfile` and `pyproject.toml`.
`deploy/`, `containers/` and `docs/workflow/` referenced none of the 17 and were
not touched. `docs/handoff/` is the evidence archive of past cards and is
deliberately left unrewritten, so it still names the pre-move modules.

The reconciliation is re-runnable and covers all 17, not the convenient ones:

```
python /tmp/f-verify.py    # 17/17 assignments fully reconciled
```

## Entrypoints and persisted names

`kb.cli` is unchanged: the container entrypoint, the service names a Quadlet
passes (`gateway`, `worker`, `model-runner`, `indexer`, `migrate`) and the
`argparse` choices are byte-identical. Only the module each name resolves to
moved:

| Service name | Module before | Module after |
| --- | --- | --- |
| `worker` | `kb.ingestion` | `kb.jobs.ingestion` |
| `model-runner` | `kb.model_runner` | `kb.jobs.model_runner` |
| `indexer` | `kb.indexer` | `kb.jobs.indexer` |
| `migrate` | `kb.migrate` | `kb.migrate` |

Persisted queue task names are data in the database, not import paths, and the
move cannot touch them. Verified by diffing the name literals across the whole
package:

```
git grep -h -oE "(name=|task_name=)[\"'][a-z_.:-]+[\"']" 31dbfdd1 -- src/kb | sort -u > before.txt
git grep -h -oE "(name=|task_name=)[\"'][a-z_.:-]+[\"']" -- src/kb | sort -u > after.txt
diff before.txt after.txt    # no output: 9 names, identical
```

## /api/v1 and the /api alias

`/api/v1` is canonical and is the only prefix in the OpenAPI schema. `/api`
stays byte-compatible and is mounted from the same router object. The alias adds
paths and adds no work: a `(method, path)` pair bound twice is refused by
`kb.http.router.assert_prefix_parity`, which is the structural form of "a POST
through the alias is executed once".

Both prefixes are answered by the same handlers, so the scope a request needs
must not depend on the prefix. `kb.access.bearer_policy.http_scope` now
normalises a path through `kb.contracts.operations.canonical_api_path` before
its frozen rules look at it. Without that, `PUT /api/v1/libraries/{id}/grants`
would have matched no `kb:admin` rule — those rules match a literal `/api`
prefix — and would have answered as `kb:contribute`. The test suite asserts
`kb:admin` under both prefixes for every administrative route.

## Route inventory, before and after

Counted from the routers themselves, at the base commit and at this one, with
the same script:

| Router | Routes before | Routes after | Identical |
| --- | --- | --- | --- |
| libraries | 8 | 8 | yes |
| projects | 7 | 7 | yes |
| members | 21 | 21 | yes |
| uploads | 3 | 3 | yes |
| content | 29 | 29 | yes |
| research | 15 | 15 | yes |
| **total** | **83** | **83** | **yes** |

The move added no route and removed none. Served under both prefixes the
application exposes 166 paths, and `tests/runtime/test_operation_registry.py`
fails if any path is bound more than once or if either prefix serves a
different verb set for the same path.

## Server-side pagination for sources, knowledge and rules

| Operation | Route | Default | Maximum | Order |
| --- | --- | --- | --- | --- |
| `libraries.sources` | `GET /libraries/{library_id}/sources` | 100 | 100 | `created_at DESC, id` |
| `libraries.knowledge` | `GET /libraries/{library_id}/knowledge` | 500 | 500 | `created_at, id` |
| `libraries.rules` | `GET /libraries/{library_id}/rules` | 100 | 100 | `created_at DESC, lineage_id, version_no DESC, id` |

Every default is the limit the catalog function already applied, so a client
that sends no parameters receives exactly the rows it received before, in the
same order, with the same bare-list body. Knowledge is the one that changed
shape of contract rather than shape of page: it had a silent `LIMIT 500` with no
way to ask for the rest. The default is still 500, and the order gained an `id`
tiebreaker so that two rows sharing a `created_at` cannot swap between pages
and make an offset page mean something different on the second read.

## The operation registry

`kb.contracts.operations` declares every product operation once: name, method,
prefix-free path, effect, scope, MCP tool, and page bounds where it paginates.
It imports nothing from `kb`, so the contract data cannot drag in FastAPI, the
database layer or the identity stack.

It is not documentation. Three things consume it and refuse to start when it
drifts:

* `kb.http.router.assert_routes_match_registry` — every declared operation
  resolves to a route the app really serves.
* `kb.mcp.server.assert_mcp_contract_registered` — every MCP tool in the frozen
  contract declares an operation, and the registry binds no tool the contract
  does not declare.
* `tests/runtime/test_operation_registry.py` — 31 node IDs covering one operation
  per MCP tool, declared scope against the handler scope table, both prefixes
  equal in method and scope, administrative routes still `kb:admin` under both,
  pagination defaults and bounds read back off the live route objects, and the
  alias schema matching the canonical one path for path.

The frozen contracts were not modified: `mcp_tools.py` and `research_tools.py`
keep their tool names, argument models and result models. The tool-to-handler
name mapping, which is the one thing the registry cannot derive on its own, is
written out in the test rather than inferred.

## Scope comes from the registered operation

The contract line is "scope is extracted by the registered operation, an
unknown protected operation is closed". At the previous candidate
`kb.access.bearer_policy.http_scope` answered, from a frozen table of URL
string patterns, and `Operation.scope` was consulted only by a test that
asserted the two agreed. That is three declarations held in agreement by
assertion, and it is the failure the registry was introduced to end.

Two changes make the table the single dispatch source.

* **The registry now covers the whole protected surface.** 33 routes — members,
  organisations, invitations, access, group grants, project bindings, library
  kinds and `/me` — were served by the application and never declared. A
  registry-first lookup would have closed every one of them. Each is now
  registered, at 83 operations, and each carries the scope it answered under
  *before*: the value was read out of the frozen pattern table rather than
  chosen, so this changes where the answer comes from and not what it is. A
  test recomputes `http_scope` for all 83 routes and asserts the registry
  agrees on every one.
* **`kb.access.bearer_policy.scope_for_request` is what dispatch calls.** A
  claimed path answers with its operation's scope. A path under the API
  prefixes that no operation claims returns `None`, and `require_scope` denies
  it — so a newly added, undeclared route now fails closed instead of
  inheriting a scope from a pattern match on its URL. A path *outside* the API
  prefixes is not a product operation and keeps the pattern answer, because
  `/auth/*`, `/mcp`, `/healthz` and `/static` were never governed by this
  table and must not start being.

`http_scope` is retained as the frozen legacy table. It is now a
cross-check in tests rather than a second source at runtime.

`unclaimed_routes` was inert and is now a real guard. It compared the full
canonical path carried by a route inventory against `OPERATION_ROUTES`, which
is keyed by the prefix-free template, so `(method, path) not in OPERATION_ROUTES`
was true for every entry: 83 served, 50 registered, 83 reported unclaimed, 50
false positives. The residual is empty, and the test asserts empty instead of
printing it.

## The shared connection factory

The contract line is a callable context manager returning `pool.connection()`,
with the lease owned by the orchestration that called it, an operation never
closing a connection it was handed, and identity established through the
existing `transaction_identity` for each SQL phase.

`kb.runtime_identity.build_connection_factory(pool)` returns the callable, and
it is the only place in the package that calls `pool.connection()` — located by
walking the AST, so a mention in prose neither satisfies nor fails the check.
The gateway builds the pool, opens it, closes it, and publishes the *factory*
on `app.state` as `db_connection`. It does not publish the pool, so there is no
object left for a route or a tool to reach past the factory with. A factory
that exists and a factory that is the only way in are different things, and
only the second one is worth having.

Every consumer leases through it: the HTTP request dependency in
`kb.http.libraries`, the MCP tool dispatch in `kb.mcp.server`, the membership
re-check in `kb.runtime_identity`, the startup embedding-profile check and the
readiness probe in `kb.gateway`. `kb.runtime_auth.install_auth` takes the
factory rather than the pool, so the identity resolver cannot hold one either.

`kb.runtime_identity.identity_connection(factory, principal)` is the per-phase
form: a fresh lease with the principal's identity established on it, released
when the block ends. It is what makes B1's short-SQL-plan → external I/O →
new-SQL-phase shape possible, because each phase opens its own lease and
re-establishes identity rather than inheriting it from whatever ran before on
that connection. It lives in this module because the two guarantees are one
guarantee: a connection is only useful here with the identity already on it,
and that identity is transaction-local.

The four test fixtures that stood a pool up for a test app now publish the
factory too, and a test asserts that no file under `tests/` re-publishes the
pool — a fixture that quietly restored it would make every integration test
pass while reopening the hole.

## The /app/* SPA fallback

The contract line is "SPA fallback only /app/*, including Reload; F prepares it
for C". F owns the route shape and the scoping; C owns the static side, and
nothing under `src/kb/static/` was touched.

Two routes in `kb.gateway`, under a literal `/app` prefix: `/app`, and
`/app/{screen_path:path}` for everything below it. They serve the same shell
the root route serves, with the asset digests already substituted, and
`Cache-Control: no-cache` so a Reload picks up a new document — that is the
"including Reload" half. Every screen address the contract names is a GET that
matches no API route and now returns the shell instead of a 404.

The scoping is the part that can do damage, so it is deliberate. A root-wide
catch-all would also answer `/api`, `/mcp`, `/auth`, `/static`, `/healthz` and
every mistyped URL with a 200 and an HTML body, and the diff that introduces
it is indistinguishable from the diff that fixes a 404. The routes are
registered before the root MCP mount, because a mount at `/` matches every
path and would otherwise swallow them first. A test drives the contract's
addresses plus a deep path, and separately asserts that the health, auth,
static, API, MCP and unknown routes do not return the shell — including the
near misses `/appx/something` and `/application`, which a sloppy prefix
boundary would swallow. The shell is not in the OpenAPI schema.

Neither fallback nor factory is live-verified behind a real listener, a real
Keycloak session or a real MCP handshake. The tests reach `create_app()` and
drive it through `TestClient` with synthetic configuration, which proves the
routing, the wiring and the scoping, not the cookie and CSRF path.
