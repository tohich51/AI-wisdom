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
| `kb.jobs` | the queue schema, ingestion, the indexer, the research worker | survives a restart: claims a task and persists the result |
| `kb.research` | the research decision — drafting, references, validation | what a research run concluded, independent of who ran it |
| `kb.adapters` | the cloud coding-agent runner | builds an argv for a process we do not own |

`kb.contracts`, `kb.access`, `kb.catalog`, `kb.retrieval`, `kb.http`, `kb.mcp`,
`kb.domain` and `kb.static` are unchanged in membership.

None of the three new packages re-exports anything from its `__init__`. A
package root that pulled in its modules would make every consumer of a single
name pay for the whole layer, and would make an import cycle possible between a
worker and the code it serves.

## The completed move map

| Before | After | Changed lines in the file |
| --- | --- | --- |
| `src/kb/queue_schema.py` | `src/kb/jobs/queue.py` | 0 |
| `src/kb/ingestion.py` | `src/kb/jobs/ingestion.py` | 4 (imports) |
| `src/kb/indexer.py` | `src/kb/jobs/indexer.py` | 0 |
| `src/kb/research_worker.py` | `src/kb/jobs/research_worker.py` | 4 (imports) |
| `src/kb/model_research.py` | `src/kb/research/model.py` | 6 (imports) |
| `src/kb/research_references.py` | `src/kb/research/references.py` | 0 |
| `src/kb/research_validation.py` | `src/kb/research/validation.py` | 0 |
| `src/kb/model_runner.py` | `src/kb/adapters/model_runner.py` | 0 |

Every other change in `src/kb` is either a new file (`contracts/operations.py`,
`http/router.py`, the three `__init__.py`) or one of the four deliberate edits
below. `git diff -M -U0 31dbfdd1 -- src/kb | grep -E '^[+-]'` shows no other
non-import line in a moved file.

Call sites updated, all in this card's lease: 21 files — 9 under `src/kb`, 11
under `tests/`, and `docs/runbooks/runtime.md` (which named
`kb.queue_schema.install_queue` and now names `kb.jobs.queue.install_queue`).
Nothing outside the lease referenced any of the eight modules, verified before
the move:

```
grep -rn "kb\.\(ingestion\|indexer\|model_runner\|queue_schema\|research_worker\|model_research\|research_references\|research_validation\)\b" \
  deploy containers spikes justfile scripts docs/workflow docs/runbooks docs/decisions
```

`docs/handoff/` is evidence of past work and was deliberately left unrewritten.

## Entrypoints and persisted names

`kb.cli` is unchanged: the container entrypoint, the service names a Quadlet
passes (`gateway`, `worker`, `model-runner`, `indexer`, `migrate`) and the
`argparse` choices are byte-identical. Only the module each name resolves to
moved:

| Service name | Module before | Module after |
| --- | --- | --- |
| `worker` | `kb.ingestion` | `kb.jobs.ingestion` |
| `model-runner` | `kb.model_runner` | `kb.adapters.model_runner` |
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
| `libraries.rules` | `GET /libraries/{library_id}/rules` | 100 | 100 | `created_at DESC, lineage_id, version_no DESC` |

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
* `tests/runtime/test_operation_registry.py` — 28 checks covering one operation
  per MCP tool, declared scope against the handler scope table, both prefixes
  equal in method and scope, administrative routes still `kb:admin` under both,
  pagination defaults and bounds read back off the live route objects, and the
  alias schema matching the canonical one path for path.

The frozen contracts were not modified: `mcp_tools.py` and `research_tools.py`
keep their tool names, argument models and result models. The tool-to-handler
name mapping, which is the one thing the registry cannot derive on its own, is
written out in the test rather than inferred.
