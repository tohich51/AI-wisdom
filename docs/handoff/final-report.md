# Final report — Knowledge Hub, MiniMax Cloud handoff

Status: **`source_bundle_with_pending_gates`** — not production-ready, not deployed.

Date: 2026-10-09. Root: `/workspace/knowledge-hub`.
Executor: MiniMax Cloud, model `MiniMax-M3.1-Flash-Preview`, thinking `max`,
provider `minimax`. No model substitution, no BYOK, no PAYG, no quota purchase.

The product is new. The database starts empty, the owner uploads their own
material, and nothing was migrated or pre-loaded. All content in this tree is
synthetic.

---

## 1. What was implemented

| Area | State | Where |
|---|---|---|
| Environment probe, capabilities, ownership | done | `docs/handoff/runtime-capabilities.json`, `ownership.json` |
| Reproducible install + real check entrypoints | done, gate proven non-vacuous | `justfile`, `pyproject.toml`, `requirements.lock` |
| Domain contract: source/knowledge/rule/experience, provenance, statuses | done, 16 tests | `src/kb/contracts/` |
| Ten MCP tool contracts, no identity in arguments | done | `src/kb/contracts/mcp_tools.py` |
| MCP SDK real roundtrip (server + client, stdio) | done, partial | `spikes/identity-mcp/` |
| Real PostgreSQL RLS: isolation, default deny, forgery, derived summaries | done, 6 tests | `tests/integration/test_rls_integration.py` |
| OpenViking index schema (accounts, generations, idempotency) | prepared, not executed | `spikes/index/001_accounts.sql` |
| Source delivery bundle + checksums | done | `dist/` |

Not implemented: C05–C32 and C33A–C35. This session stopped at C04/C36. The
remaining cards are ordinary product work that needs a runtime this environment
does not have; see §4.

## 2. What actually ran, and on what

Every row below is a real command in this cloud workspace with its real exit
code. Full log: `docs/handoff/evidence/gate-run.txt`.

| Command | Exit | Result |
|---|---|---|
| `python3 verify_handoff.py` | 0 | 65 files, 42 tasks, 18 packages, 20 access cases |
| `just prepare` | 0 | installs from the pinned lock; mcp/fastapi/psycopg/pydantic all import |
| `just check` | 0 | ruff lint + format, mypy on 8 source files, 16 unit tests |
| `just verify-self` | 0 | injected defect was rejected, tree returned green |
| `just integration` | 0 | 6 RLS tests pass on a **real PostgreSQL 16.2** |
| `just acceptance` | 5 | **not run** — no acceptance-marked tests exist yet (C33 scope) |
| `just package` | 0 | 20-file source zip, sha256 recorded, nothing pushed |
| `scripts/quality/check_gates.py` | 1 | gates unmet, by design |

### The gate is not decorative

`just verify-self` writes a real defect into the tree, requires `just check` to
reject it, then confirms the tree is green again. If that recipe ever passes
while the defect survives, the gate is fake. It was run twice — once at C01 and
once after C02 — and both times the defect was caught.

### Verified on real PostgreSQL, not mocks

A real PostgreSQL 16.2 server (`pgserver` 0.1.4, actual server binaries) was
started and queried through a role that is neither `SUPERUSER` nor `BYPASSRLS`:

- tenant sees only its own rows
- unset context is **default deny**, not "everything"
- a forged row owned by another principal is rejected by `WITH CHECK`, and the
  table row count is unchanged afterwards
- a derived aggregate table is filtered too, so shared summaries cannot mix
  audiences

## 3. Findings worth carrying forward

These are real incompatibilities discovered by running things, not by reading
about them.

1. **mcp 2.3.0 renamed `FastMCP` to `MCPServer`.** `mcp.server.fastmcp` does not
   exist in 2.x. Any v1-era example or snippet will fail. `InitializeResult`
   exposes `server_info`, not `serverInfo`.
2. **`SET app.user = 'x'` is a syntax error** — `user` is reserved. The RLS
   context GUC must use a non-reserved name (`app.principal`).
3. **`current_setting()` returns `text`.** Comparing it to a `uuid` column needs
   an explicit `::uuid` cast, otherwise the policy errors at runtime.
4. **PostgreSQL data directories cannot live on the workspace NAS mount.**
   `initdb` fails there; the test fixture uses a local temporary directory.
5. **`pgserver.psql` returns stdout only** and lets errors go to the console, so
   a policy rejection is invisible to an assertion. The fixture uses
   `ON_ERROR_STOP=1` with captured stderr.
6. **OpenViking 0.4.23 needs 126 dependencies and an embedding provider.** With
   no Ollama and paid APIs forbidden, it cannot be started in this sandbox.

## 4. What failed, skipped, or could not run

- **No container runtime, no systemd.** `podman` and `docker` are absent (exit
  127) and PID 1 is `node`. Quadlet was neither run nor claimed.
- **No Keycloak.** No JVM. Token issuance, audience rejection, per-device
  refresh — all unverified.
- **No OpenViking runtime.** Blocked on an embedding provider, not on effort.
- **No second independent MCP client.** Only the official SDK client exists here.
- **A01–A20 and S01–S04 were not executed.** The PostgreSQL leg is now proven;
  the Keycloak and OpenViking legs are not. `just acceptance` exits 5 on purpose.
- **No product model-runner exists here.** Ordinary code paths use a fake
  adapter. The real subscription call is E02 and remains pending.

### Owner gates

| Gate | What | Here |
|---|---|---|
| E01 | rootless Podman + Quadlet + systemd | **blocked** — not installed, PID 1 is `node` |
| E02 | product subscription model-runner, one synthetic call | **pending** — coding-session entitlement is not the product's |
| E03 | real PG/Keycloak/OpenViking + 2 MCP clients | **partial** — PG leg proven; KC and OV absent |
| E04 | backup/restore to a separate target | **pending** |
| E05 | server inventory, domain/issuer, clean install | **pending** — no SSH, no owner server |

## 5. Where the source is

- `dist/knowledge-hub-source.zip` with `dist/SHA256SUMS` and
  `dist/source-manifest.json` (rebuild: `just package`)
- git history in ROOT; every card has a commit
- `docs/handoff/results/{C00,C01,C02,C03,C04}.json`
- `docs/handoff/checkpoint.json` — resume point
- `docs/handoff/evidence/gate-run.txt` — raw command output

Nothing was pushed, published, deployed or mailed. No owner server was
contacted. No secrets are in the tree; the packager refuses paths matching
credential patterns.

## 6. Next step

Exact command, from this tree:

```bash
just prepare && just check && just integration
```

Then, in order: a real Keycloak on a host with a JVM (E03), a CPU embedding
provider for OpenViking (C04 runtime), and only then C05 onward on a machine
with rootless Podman and systemd (E01). C33A–C33D can be written against the
real PostgreSQL leg proved here, with the Keycloak and OpenViking legs marked
pending until those services exist.

## 7. Acceptance

`review_status` is `not_reviewed` in every result file and was not set to
accepted by this agent. Independent acceptance is Codex's or the owner's.
