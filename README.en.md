# AI-wisdom

[Русский](README.md) · English

An **ИИдея** project: independent development, AI research, prototypes, and
practical products. AI-wisdom was developed through Hermes.
[Studio website](https://aisaassolution.com) ·
[Telegram @aisaassolution](https://t.me/aisaassolution).

A self-hosted knowledge library for an owner and invited colleagues: keep source
provenance, retrieve published knowledge, curate versioned rules, and record
explicit use and outcomes. The browser UI and authenticated MCP endpoint share
the same catalog and access rules. You supply the content; no books, accounts,
credentials, or populated databases are included.

**Public beta:** Python distribution version `0.1.0b1`, release tag
`v0.1.0-beta.1`. These are release identifiers, not a verification receipt.
The seven-service stack uses rootless Podman + Quadlet. It is not a portable,
one-command installer or a production-stability guarantee. See the
[beta installation guide](docs/public-beta.md) for operator setup and checks.

## What the beta does

- Ingest PDF, EPUB, DOCX, HTML, and text originals with provenance. Scanned PDFs
  report `needs_ocr`; this is not an OCR service.
- Extract draft knowledge and rules through an explicitly requested model job.
  Generated results require curator approval before publication.
- Retrieve published material using lexical and vector search, without a
  generative model call. Unavailable or invalid vector projections are disclosed
  as lexical fallback.
- Keep published rule versions immutable; corrections create new versions.
  Exporting `SKILL.md` neither installs nor runs it.
- Record application and outcomes explicitly. Retrieval means “served,” not
  “used,” “successful,” or “proven.” Unknown metadata stays unknown.

Typed semantic relationships and embedding similarity are different views:
similarity is not a verified relationship. Global entity merging and clustering
are not implemented. Audio, video, and slide generation are not implemented.

## Architecture and trust

```text
Browser / MCP client -> gateway -> PostgreSQL (authoritative rights and records)
                           |          ^
                           |       worker -> MiniMax subscription runner
                           |          |
                           +-> OpenViking <- single indexer
                                  |
                           OpenRouter embeddings
Keycloak OIDC -> authenticated identity at the gateway
```

PostgreSQL is the authority for library rights, provenance, published versions,
and experience. Runtime roles are non-superuser, have no `BYPASSRLS`, and use row
level security; missing identity is denied. Library kind and audience are
independent, and project membership does not grant access to linked libraries.

Keycloak provides OIDC authentication. OpenViking is a rebuildable `vectors_only`
projection, not an authorization database. The embedding profile is OpenRouter
`qwen/qwen3-embedding-8b`, 4096 dimensions. Product generation uses a separate
MiniMax subscription runner with `MiniMax-M3.1-Flash-Preview`, not an automatic
API-key or pay-as-you-go fallback. Bring your own authorized provider accounts
and credentials; subscription limits still apply. The external CLI's licensing
boundary is documented in the installation guide; it is not bundled here.

## Clean-clone package and CLI checks

Use Linux x86_64, Python **3.11 or 3.12** with `venv` and pip, Git, Bash, and
[`just`](https://github.com/casey/just). Network access to the Python package
index is required. These commands install the package and development checks;
they do **not** start the full stack or configure authentication.

```sh
git clone https://github.com/tohich51/AI-wisdom.git
cd AI-wisdom
git checkout --detach v0.1.0-beta.1
git rev-parse HEAD
python3 --version                 # must be 3.11.x or 3.12.x
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e '.[dev,pgtest]'
.venv/bin/python -m pip check
.venv/bin/kb --help
just check
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
just package
```

An unavailable tag is an error, not permission to silently validate a different
branch. Release-owner pre-publication checks use the exact assembled candidate
commit instead. `requirements.lock` is the dependency pin set; the Hatchling
build backend is not claimed to be bit-for-bit reproducible. `just package`
builds a source delivery bundle, not a running deployment.

`just integration` requires real PostgreSQL. `just acceptance` requires its real
service prerequisites and runtime evidence; neither is implied by `just check`.
Mandatory gates must fail closed when they cannot run. Do not turn missing
services, skipped checks, or absent owner receipts into a passing release claim.

## Install the real stack

Read [public beta setup](docs/public-beta.md) before activation. It covers the
seven services, local image builds, existing-database migration/queue/first-owner
bootstrap, subscription launcher, OIDC/provider credentials and template mapping.

- [Product Quadlets](deploy/quadlet/README.md): gateway, worker, model runner, indexer.
- [Infrastructure Quadlets](deploy/xorek/README.md): PostgreSQL, Keycloak, OpenViking.
- [Runtime runbook](docs/runbooks/runtime.md): migration and operational safeguards.
- [Optional SDK MCP client](clients/mcp/README.md): separate client setup and checks.

Deployment/runbook examples can retain installation-specific paths. Use the
portable mappings in the beta guide; never copy another operator's secrets.

## Limits and license

This beta has no established production-stability or hour-long load-test claim.
There is no 1–2 second cold-latency guarantee. Review the
[limitations and release checks](docs/public-beta.md#limitations-and-release-checks)
before relying on it.

AI-wisdom is [MIT licensed](LICENSE). That license does not relicense external
CLI packages or include inference, credits, subscriptions, or rights to uploaded
content. You are responsible for those accounts, terms and rights.
