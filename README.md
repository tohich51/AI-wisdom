# AI-wisdom

Project knowledge base: a closed library for the owner plus up to 10 invited
colleagues. Libraries are created by the owner; files and URLs are uploaded by
the owner. There is no preloaded content and no migration of an older base.

## Status

**`source_bundle_with_pending_gates`** — source only, not deployed.

This tree was developed in an isolated cloud sandbox with **no container
runtime and no systemd**, so Podman/Quadlet (E01) could not be executed. No
production deployment, no published image, no public endpoint.

| Gate | What | Here |
|---|---|---|
| E01 | rootless Podman + Quadlet + systemd | **blocked** — no podman/systemd in the dev environment |
| E02 | product subscription model-runner | **pending** — no product runner exists; fake adapter used |
| E03 | real PG / Keycloak / OpenViking + 2 MCP clients | **partial** — real PostgreSQL 16.2 and RLS verified; Keycloak and OpenViking absent |
| E04 | backup/restore to a separate target | **pending** |
| E05 | server inventory, domain/issuer, clean install | **pending** |

Detail: `docs/handoff/runtime-capabilities.json` and
`docs/handoff/final-report.md`.

## The model

```
source ──▶ provenance ──▶ knowledge ──▶ rule (immutable version) ──▶ experience
```

A **retrieved** result means *served*. It does not mean applied, and it does
not mean the outcome is known. An outcome appears only after a user, an agent
or a metric reports one; a correction is a new event. Missing attributes stay
`NULL` — they are never invented.

Library *kind* (`reference`, `brand`, `project`, `playbook`, `experience`) and
its *audience* are independent axes. Membership in a project library does not
grant access to the libraries that project links.

## Access

PostgreSQL is the authority. The gateway never decides access in Python and
then filters — a filter in application code is a filter that can be forgotten.
`src/kb/access/policy.py` mirrors the SQL rules so the application can fail
early with a clear error; where the two ever disagree, the SQL wins and the
mismatch shows up as denied rows.

`kb_app` is the runtime role: not superuser, not a table owner, no `BYPASSRLS`,
with `FORCE ROW LEVEL SECURITY`. Identity is transaction-local (`app.principal`,
set by the transport), never taken from a request payload. A missing or
malformed principal resolves to no role, which is default deny.

Worth knowing when reading the tests: an `UPDATE` under RLS is **filtered**
(`UPDATE 0`, no error) while an `INSERT` is **rejected** by `WITH CHECK`. Code
must never read a zero row count as success.

## Commands

```bash
just prepare      # reproducible install from the lock
just check        # lint + types + unit tests, no services
just integration  # real PostgreSQL: RLS, access model, empty bootstrap
just acceptance   # A01-A20 + S01-S04
just package      # build the source bundle (never deploys)
just verify-self  # prove that `check` really fails on a broken tree
just env          # what this environment can and cannot do
```

A mandatory gate that cannot run exits **nonzero**. `just acceptance` exits 5
with an explicit NOT RUN message when no acceptance tests exist, because the
absence of tests is not a passing test.

`just verify-self` injects a real defect and requires `just check` to reject
it, then confirms the tree is green again. If it ever passes while the defect
survives, the gate is fake.

`just` lives in `.tools/` (persistent, NAS-backed), not `/usr/local/bin` —
the sandbox container is recreated between sessions and wipes the latter. Put
it on PATH: `export PATH="$PWD/.tools:$PATH"`.

## Layout

```
src/kb/contracts/   single source of schema for API + MCP
src/kb/access/      the one access check
migrations/         DDL, single owner
deploy/             target deployment units (Quadlet)
tests/              unit | integration | acceptance
spikes/             compatibility probes kept as evidence
docs/decisions/     dependency and domain decisions
docs/handoff/       capabilities, checkpoint, per-card results, evidence
```

## Verified on real infrastructure, not mocks

- **PostgreSQL 16.2** (`pgserver` ships real binaries) — RLS isolation,
  default deny, `WITH CHECK` rejection of forged rows, per-library roles,
  published-rule immutability, project-membership isolation
- **MCP SDK 2.3.0** — real server and client in separate processes over a real
  stdio transport; a spoofed identity argument is ignored

## Environment notes

Developed on Debian 12, Python 3.11.2, 2 vCPU, 3 GiB RAM, NAS-backed
workspace. Target for deployment is Linux x86_64 with rootless Podman +
Quadlet, which this environment is **not**.

`pip` here needs `--index-url https://pypi.org/simple/` plus `--trusted-host`;
`just prepare` passes them. Harmless elsewhere. The pip-packaged PostgreSQL
ships only `plpgsql` and `vector`, so the schema uses the core
`gen_random_uuid()` (PG13+) rather than requiring `pgcrypto`.

## Not included

No `node_modules`, no `.venv`, no runtime databases, no OAuth material, no
tokens, no private keys, no real books. `scripts/build_bundle.py` refuses to
package anything matching a credential pattern.

Independent acceptance is Codex's or the owner's; every result file says
`review_status: not_reviewed` and this agent does not set it to accepted.
