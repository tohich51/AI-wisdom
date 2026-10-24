# Public beta installation guide

This guide accompanies [AI-wisdom](../README.md) beta `0.1.0b1`, with release
tag `v0.1.0-beta.1`. These identifiers do not attest publication or acceptance.
It separates a package/CLI check from a configured service deployment. A working existing deployment is not proof of a clean install
on another host. No one-command full-stack installer is provided.

## Choose an installation mode

| Mode | What it establishes | What it does not establish |
|---|---|---|
| Package/CLI smoke | Python dependencies install, CLI loads, static/unit checks run, wheel and source bundle build | Database initialization, OIDC login, providers, containers, backup, runtime acceptance |
| Full stack | Operator-configured PostgreSQL, Keycloak, OpenViking and four product services on rootless Podman/Quadlet | Automatic provisioning, external CLI licensing, production stability, universal acceptance |

Use the [README commands](../README.md#clean-clone-package-and-cli-checks) first.
They target Linux x86_64 and Python 3.11–3.12 with `venv`, pip, Git, Bash and
`just` installed. Use a fresh checkout and environment. Record the checked-out commit with `git rev-parse HEAD`. The release version is
`0.1.0b1`; the Git tag is `v0.1.0-beta.1`. Before tag publication, the release owner
validates the exact assembled commit, without treating the default branch as a
substitute for that candidate.

`just prepare` is the repository shortcut for creating `.venv`, installing
`requirements.lock`, and installing `.[dev,pgtest]`. It also includes
sandbox-specific pip `--trusted-host` options. The README uses normal HTTPS pip
commands instead; do not disable certificate verification to conceal a network
or certificate problem. The `pgtest` extra supplies native PostgreSQL test
binaries, not the production database service.

## Full-stack prerequisites

Provision these yourself before starting product services:

- Linux x86_64 with a working systemd user manager, rootless Podman and Quadlet.
  The existing deployment uses Podman 4.9.x. Verify generated units on your own
  distribution; a Docker-only environment or a Python virtualenv is insufficient.
- Persistent storage for PostgreSQL, source material, and the vector projection;
  an independent backup/restore target. Resource limits in individual units are
  not a host memory budget or a minimum-sizing guarantee.
- Your own DNS and HTTPS reverse proxy for the app and OIDC issuer, for example
  `https://kb.example.com` and `https://idp.example.com/realms/example-realm`.
  Keep database/model endpoints private and published container ports loopback-only.
- PostgreSQL and administrative credentials for one-shot schema/bootstrap work,
  plus separate least-privileged runtime roles. Keep the identity database
  separate from the product database.
- Keycloak, a realm, clients, verified users, claim mappings and redirect/logout
  configuration. Configure your own SMTP service if account invitation or
  recovery flows require mail. No working identity account or database is bundled.
- An OpenRouter account, credentials and access to
  `qwen/qwen3-embedding-8b` at **4096 dimensions**. Provider inference may cost
  money; it is not supplied by the repository or a MiniMax subscription.
- An authorized MiniMax subscription/OAuth setup for the product generation
  runner, using `MiniMax-M3.1-Flash-Preview`. Its host launcher integration is a
  separate prerequisite, described below. Do not substitute an API-key/PAYG
  route or promise unlimited subscription calls.
- Access to upstream container registries and the Python package index. The
  application image must be built locally for the selected release; a
  `localhost/...` image name is not a downloadable public image.

You must have rights to upload and process your own material, including sending
relevant content to external inference providers. Do not upload secrets or
private material as installation test fixtures.

## Build the images, not a deployment

After the README installation and `just check`, build the application wheel:

```sh
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

The [application Containerfile](../containers/Containerfile) requires
`requirements-runtime.lock`, `dist/*.whl`, `migrations/`, and an explicit
`PYTHON_IMAGE` build argument. Use a fresh checkout so `dist/` contains only the
intended application wheel. The base must provide Python 3.11 or 3.12 and Debian
`apt-get`; an arbitrary Alpine image will not satisfy this Containerfile. Obtain
and validate a suitable Linux amd64 base-image digest before building.

Run the following only after setting `PYTHON_IMAGE` to that approved,
digest-pinned image reference. It intentionally fails if the variable is unset:

```sh
: "${PYTHON_IMAGE:?Set an approved digest-pinned Python 3.11/3.12 Debian image}"
RELEASE_SHA="$(git rev-parse HEAD)"
podman build --platform linux/amd64 \
  --ignorefile containers/.containerignore \
  --build-arg "PYTHON_IMAGE=$PYTHON_IMAGE" \
  -f containers/Containerfile \
  -t "localhost/ai-wisdom:$RELEASE_SHA" .
```

The runtime dependency install uses `--require-hashes`. Preserve that check.
The wheel build backend is not fully locked, so this guide does not claim a
bit-reproducible image. Building an image is not acceptance or activation.

The [OpenViking Containerfile](../containers/openviking-vectors-only.Containerfile)
pins its upstream image by digest and applies exact source-hash/anchor-checked
patches. Build that patched image separately, for example:

```sh
podman build --platform linux/amd64 \
  -f containers/openviking-vectors-only.Containerfile \
  -t "localhost/ai-wisdom-openviking:$RELEASE_SHA" .
```

This is a local tag chosen for this example, not a published artifact. Make the
OpenViking unit refer to the exact image you built and validated; do not assume
its current template already uses this tag. A patch mismatch must stop the build,
not be bypassed by editing the hash check or silently using an unpatched server.

## Configure identities, secrets and providers

Keep filled configuration outside Git and outside images. The current unit
convention is `%h/ai-wisdom-config/` (the service user's home), with secret files
restricted to that user, typically mode `0600`. Inspect and adapt every referenced
file, mount, executable and environment variable before installing a unit.
Do not mount an entire home directory or a container-engine socket.

Use these tracked examples as field references, not ready-to-run configurations:

- [OIDC environment](../deploy/identity/oidc.env.example): issuer, client,
  audience, redirects, session key and optional mail settings. Use your own
  realm and account values; the guide's `example.com` URLs are placeholders.
  Register matching HTTPS redirects and enforce the expected identity/account
  claims. Keep secure cookies enabled for deployed service.
- [Embedding provider environment](../deploy/embeddings/provider.env.example):
  deliberately `unconfigured` with semantic search/indexing disabled. It does
  not reflect the configured deployment until the operator fills it. Verify
  the model, dimension and credentials before creating any index. Changing
  the vector profile requires a new complete index generation.
- [Semantic environment](../deploy/embeddings/semantic.env.example): split
  gateway read keys from indexer/provisioner credentials. Do not give the gateway
  provider, root, bootstrap or index-writing credentials.
- [Index provisioning environment](../deploy/maintenance/kb-provision-index.env.example):
  private database and service-key paths; placeholders are not usable secrets.

The examples do not form a complete generated configuration set. PostgreSQL,
Keycloak, OpenViking configuration and the four product environment files still
need operator provisioning. Missing or unresolved values must remain errors.

### Subscription-only MiniMax launcher

The repository provides `scripts/mmx-subscription.cjs`, not the MiniMax runtime
or an authenticated profile. It requires Node.js >=18 and an explicit absolute
`MMX_RUNTIME` pointing to `mmx-cli@1.0.25/dist/mmx.mjs`. The wrapper checks package
name/version, rejects API-key/base-URL/config/update/verbose operations, strips
API-key/base-URL/verbose environment settings, checks OAuth status privately and
performs at most one bounded refresh before generation. Missing/invalid auth
fails closed. Status and refresh output are discarded; `auth login` alone is
interactive and displays the provider's authorization instructions. No live
model call is needed to install or run the offline tests.

External dependency licensing boundary: the public npm package identifies itself
as `mmx-cli` version `1.0.25`, points to
[MiniMax-AI/cli](https://github.com/MiniMax-AI/cli), and requires Node >=18. Its
published package metadata has no `license` field, its four-file tarball contains
no license, and the upstream LICENSE/license API lookup returned 404 during this
producer check. Do not infer an open-source grant from public availability or
from a bundled dependency's license comment. No CLI/runtime is redistributed
with AI-wisdom. The operator/release owner must establish applicable vendor
permission/terms before adopting this dependency; this is not a licensing
recommendation or a claim that the check passed.

For an operator who has established that permission, the exact pinned install
and own-account login procedure is:

```sh
# Choose your own absolute installation path; not another operator's home.
MMX_PREFIX="$HOME/ai-wisdom-tools/mmx-1.0.25"
npm install --prefix "$MMX_PREFIX" mmx-cli@1.0.25
export MMX_RUNTIME="$MMX_PREFIX/node_modules/mmx-cli/dist/mmx.mjs"
install -m 0755 scripts/mmx-subscription.cjs "$MMX_PREFIX/mmx-subscription"
install -d -m 0700 "$HOME/ai-wisdom-auth"
HOME="$HOME/ai-wisdom-auth" "$MMX_PREFIX/mmx-subscription" auth login
HOME="$HOME/ai-wisdom-auth" "$MMX_PREFIX/mmx-subscription" auth status
```

A successful `auth status` exits zero without printing account/status JSON.
Complete login as the dedicated service user, using that user's own authorized
subscription and isolated HOME; do not copy an existing user's `.mmx` state.
Keep it writable for refresh and private to the service user. Never mount this
state into the gateway, worker, or indexer. Do not set API credentials or a
custom provider endpoint in this profile. The wrapper is a route guard for a
trusted, operator-controlled runtime/config, not a sandbox against a malicious
runtime or modified config. Its pin check is not a cryptographic attestation.

### Map the model-runner template to your host

The application reads `KB_MINIMAX_CLI` (absolute executable path) and
`KB_MODEL_RUNNER_TOKEN` (at least 32 characters). The token is shared only with
the authorized worker and is removed from the CLI child's environment.
`MMX_RUNTIME`, `HOME` and `PATH` are inherited by the wrapper. There is no
`--launcher` app flag. Keep the product model `MiniMax-M3.1-Flash-Preview`.

Replace the four launcher/auth `Volume=` entries and the existing `HOME`/`PATH`
entries in `deploy/quadlet/pkb-model-runner.container` with this explicit generic
mapping (choose host directories and a Linux amd64-compatible Node binary):

```ini
Volume=%h/ai-wisdom-auth/.mmx:/opt/ai-wisdom-auth/.mmx:rw
Volume=%h/ai-wisdom-tools/node:/opt/ai-wisdom-tools/bin/node:ro
Volume=%h/ai-wisdom-tools/mmx-1.0.25/mmx-subscription:/opt/ai-wisdom-tools/bin/mmx-subscription:ro
Volume=%h/ai-wisdom-tools/mmx-1.0.25/node_modules:/opt/ai-wisdom-tools/node_modules:ro
Environment=HOME=/opt/ai-wisdom-auth
Environment=PATH=/opt/ai-wisdom-tools/bin:/usr/local/bin:/usr/bin:/bin
Environment=KB_MINIMAX_CLI=/opt/ai-wisdom-tools/bin/mmx-subscription
Environment=MMX_RUNTIME=/opt/ai-wisdom-tools/node_modules/mmx-cli/dist/mmx.mjs
```

Log in with an isolated host HOME whose `.mmx` corresponds to the first mount
(e.g. `HOME="$HOME/ai-wisdom-auth"` for the login/status commands above). Provision
that directory with mode 0700. The Node binary must run with the image's libc
and architecture; a macOS binary will not. Keep all runtime package dependencies
in the read-only node_modules mount, rather than copying only the entry file.
Put the worker token in the private `model-runner.env`, not the tracked unit.
Preserve the existing network isolation, read-only root, private tmpfs and lack
of a published model-runner port. The application Containerfile does not install
Node, npm, the external CLI or OAuth state.

## Database migration and first owner

Start and provision your own isolated PostgreSQL container/database first. Use
its administrative connection through a private socket or loopback-published
port reachable from the machine running this command. Do not use container
PGDATA as a native `--pgdata` directory. Provision the separate Keycloak database
and least-privileged product runtime credentials independently.

Load `KB_ADMIN_DATABASE_URL` from your secret manager into the current process
environment, without printing it, putting it on argv, or enabling shell tracing.
The DSN must identify the intended product database and a one-shot administrator
capable of migrations/role grants/queue installation, not gateway credentials.
For a fresh product database, the single supported bootstrap command is:

```sh
: "${KB_ADMIN_DATABASE_URL:?Load the private administrator DSN first}"
export KB_ADMIN_DATABASE_URL
PYTHONPATH=src .venv/bin/python scripts/bootstrap.py \
  --dsn-env KB_ADMIN_DATABASE_URL \
  --organisation example-organisation \
  --owner-principal VERIFIED_PRINCIPAL_UUID \
  --owner-issuer https://idp.example.com/realms/example-realm \
  --owner-subject VERIFIED_SUBJECT --json
unset KB_ADMIN_DATABASE_URL
```

Replace the principal/subject with your verified identity coordinates. The
command applies all checksummed migrations through `kb.migrate.apply_migrations`,
then calls `kb.jobs.schema.install_queue` once, then binds the first owner with
the existing locked transaction. Queue installation is idempotent, checks its
schema checksum, and grants only the intended runtime roles. No separate queue
SQL or native `psql` invocation is needed. A missing/empty DSN, missing identity
pair, half pair, or invalid principal is refused before database work. The DSN
route does not import/start pgserver and has no fallback to a native database.

Both identity flags are mandatory in DSN mode. Exact active bindings are
idempotent; a different identity/administrator or deactivated membership is
refused, never reset/reactivated. The JSON report contains organisation/principal
identifiers and actual counts, not the DSN or issuer/subject. Treat the report as
private installation evidence. A fresh installation has zero content: bootstrap
does not create a user, password, library or source. A repeat preserves and
reports existing content instead of deleting it. Migration, queue and owner
binding are separate phases; if a later phase fails, earlier successful schema
work remains and can be safely retried after resolving the cause.

For explicitly selected native test PostgreSQL, use `--pgdata /absolute/path/to/new/product-pgdata`
instead of `--dsn-env KB_ADMIN_DATABASE_URL` with the same owner flags. These two
options are mutually exclusive. Omitting both retains the historic native
default `/tmp/kb-bootstrap-pg`; native mode without both identity flags retains
organisation-only setup and reports `owner_setup_required=true`. Prefer explicit
selection. Native mode requires the `pgtest` extra. Do not run first-owner setup
as an upgrade/reset procedure or grant runtime roles administrative privileges.

For a schema-only upgrade, the inspected CLI is:

```sh
# KB_ADMIN_DATABASE_URL must already be securely loaded and exported.
.venv/bin/kb migrate --directory migrations
```

The migration CLI is not first-owner or queue setup. Never weaken checksum
validation or replace failed runtime gates with package-only checks.

## Install the explicit Quadlet set

Consult [product units](../deploy/quadlet/README.md) and
[infrastructure units](../deploy/xorek/README.md). These are host-derived
templates, not an automatic secure installation. Review them locally first;
do not copy historical units with a wildcard.

Install only these seven reviewed/adapted container files plus their network into the service user's
`~/.config/containers/systemd/`:

| Source directory | Files |
|---|---|
| `deploy/xorek/` | `pkb.network`, `pkb-postgres.container`, `pkb-keycloak.container`, `pkb-openviking.container` |
| `deploy/quadlet/` | `pkb-gateway.container`, `pkb-worker.container`, `pkb-model-runner.container`, `pkb-indexer.container` |

The four product units reference `localhost/ai-wisdom:__RELEASE_SHA__`.
Replace that placeholder with the exact locally built and accepted image tag;
do not activate unresolved templates. Inspect infrastructure image references
as well, especially the patched OpenViking image. Review all host-specific
paths, secrets, persistent volumes and resource limits.

After installation, regenerate the user units:

```sh
systemctl --user daemon-reload
```

Validate generated units before starting anything. The intended dependency
order is PostgreSQL/Keycloak before gateway, PostgreSQL/model-runner before
worker, and PostgreSQL/OpenViking before indexer. Maintain one index writer and
one generation at a time. There is no Ollama/embedding sidecar in this route.
Verify autostart, user-manager lifetime, restart and readiness on your host;
`WantedBy=default.target` alone is not proof that services survive logout/reboot.
Expose only the intended app and identity HTTPS endpoints through your proxy.

The linked runtime runbook also contains operator-specific backup details.
Adapt those to your own encrypted independent target; do not treat a private
mount path as a portable default. Test restoration into an isolated database
and verify ACL/version data. Do not put OAuth material into images, public
receipts or handoffs.

## Verify before calling it installed

Run and preserve real results for the exact candidate:

```sh
just check
just integration
just acceptance
just package
```

`check` is static analysis plus service-free unit tests. `integration` requires
real PostgreSQL. `acceptance` exercises access/scaling scenarios and can depend
on live services and owner runtime receipts. It is not universally runnable
from a clean clone without that setup. An unavailable mandatory check is a
failure/pending gate, not success. Do not suppress exit codes, replace real
services with mocks for runtime acceptance, or treat missing tests as passing.

Separately verify HTTPS/OIDC login and claim mapping, denied access across
libraries, persistence, migrations and queue setup, draft extraction/approval,
lexical and semantic retrieval, restart behavior, and backup restoration. Do not
use an HTTP response or a running container count as the whole acceptance test.
Use the [optional SDK MCP client guide](../clients/mcp/README.md) for its own
setup and validation; its implementation is maintained separately, and no client
commands are assumed here.

## Limitations and release checks

- Beta version/tag identifiers do not claim production stability or completed
  end-to-end release acceptance. The release owner verifies the assembled tree,
  including the separately maintained parser/job changes and optional MCP client.
- An existing seven-service deployment is reported; clean installation on a new
  host is not established by those deployment results. Operator configuration, external CLI permission,
  provider access and real-service acceptance remain installation prerequisites.
- Retrieval is not confirmed use. Outcomes need an explicit report; generated
  knowledge and rules remain drafts until approved. Invalid model answers do not
  become knowledge. An interrupted uncommitted call may consume another
  subscription call when retried.
- Typed semantic relations are not embedding-similarity edges. Global entity
  merging/clustering and audio/video/slide generation are not implemented.
  Scanned PDFs require a separate OCR path.
- There is no 1–2 second cold-latency guarantee. Measure your own accepted
  deployment; a component benchmark is not an end-to-end installation guarantee.
- No hour-long load-test or blanket acceptance claim is made. Open-source
  software does not make external inference free or subscriptions unlimited.

### Focused portability regressions

```sh
.venv/bin/python -m pytest -q tests/unit/test_bootstrap_portability.py
node --test tests/unit/mmx-subscription.test.cjs
# Real isolated PostgreSQL (pgtest extra); retain the existing regressions too:
.venv/bin/python -m pytest -q tests/runtime/test_bootstrap_dsn.py tests/runtime/test_clean_bootstrap.py
```

Wrapper tests use a synthetic child runtime and no provider calls. The DSN
runtime regression provisions an isolated real PostgreSQL fixture, then passes
its connection to bootstrap and forbids native startup inside that route. It
checks empty counts, migrations/queue, repeat binding, no-identity no-writes,
and deactivation refusal. It is not a seven-service acceptance substitute.

### Release-owner verification receipt

The release owner fills this receipt for the assembled candidate; blank/pending
fields are not successful checks:

- Exact commit and package version: pending root verification.
- Clean-clone package/CLI/check/wheel/source-bundle results: pending.
- Focused tests and existing real-PostgreSQL regressions: pending root run.
- Seven-service/OIDC/provider/persistence/restore acceptance: pending.
- External CLI usage/redistribution permission decision: pending.
- Independent review and release/tag publication record: pending.

No tag, publication, service activation or production deployment is established
by this document. Root owns the final receipt and release decision.
