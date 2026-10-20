# deploy/quadlet — product unit templates

The four whitelisted product units, mechanically synchronized from the current
rootless units on the accepted server (source `65a6`, source gates 1554 passed).

All four use `localhost/ai-wisdom:__RELEASE_SHA__`. `__RELEASE_SHA__` is a
required placeholder: replace it **only** after an exact `linux/amd64` image has
been built and passed acceptance on that host. Until then these templates are not
runnable as-is. Infrastructure pins live in [deploy/xorek](../xorek/README.md).

## Install (explicit list — no wildcard)

Install into `~/.config/containers/systemd/`:

| File | Unit | Responsibility | Published ports | Limits as written |
|---|---|---|---|---|
| `pkb-gateway.container` | `pkb-gateway.service` | HTTP entrypoint | `127.0.0.1:19280:8080` | `MemoryMax=512M`, `CPUQuota=100%` |
| `pkb-worker.container` | `pkb-worker.service` | document processing and draft extraction | none | `MemoryMax=1536M`, `CPUQuota=100%` |
| `pkb-model-runner.container` | `pkb-model-runner.service` | model runner with its specific runner OAuth path | none | `MemoryMax=768M`, `CPUQuota=50%` |
| `pkb-indexer.container` | `pkb-indexer.service` | the single index writer (published knowledge only) | none | `--memory=768m`, `--cpus=1`, pids 256 |

Dependency order is declared in the units themselves: gateway after
postgres+keycloak, worker after postgres+model-runner, indexer after
postgres+openviking. There is one index writer and one product generation; the
current semantic path uses external OpenRouter qwen embeddings (4096-dim) — no
Ollama or embedding sidecar. All four autostart via `WantedBy=default.target`.

## Host-specific prerequisites

`pkb-model-runner` mounts only the runner OAuth path (`~/.mmx` plus the pinned
`node`, `mmx-subscription` and `ai-wisdom-tools` files). No sockets and no
whole-home mounts. That credential path is per host: the source handoff is
complete, but the owner must complete the E02 OAuth step on **each** new host, as
described in [docs/runbooks/runtime.md](../../docs/runbooks/runtime.md).

Closed env files (`gateway.env`, `worker.env`, `model-runner.env`,
`indexer.env`) must already be provided under `%h/ai-wisdom-config/`; secret
values never appear in these files or in this README.

## Legacy files

The legacy `gateway.container` is narrowed to a loopback publish to prevent
all-interface exposure. That is a root-template correction only — the running
production server is unchanged. Older unnamed files still present in this
directory are historical/unvalidated: do not install them; deletion is a separate
decision.

This documentation describes the accepted current server; installation on a new
host is **not** verified here.

See also [deploy/xorek](../xorek/README.md) and
[deploy/maintenance](../maintenance/README.md).