# deploy/xorek — infrastructure unit templates

Rootless quadlet templates synchronized from the **currently accepted server**
(source `65a6`, rootless Podman 4.9.3, `systemd --user`, UID 1003 `kb-inspect`).
These are the infrastructure units; they contain no `__RELEASE_SHA__` placeholder.
Upstream image digest pins and the accepted OpenViking source pin are fixed here.

## Install (explicit list — no wildcard)

Install into `~/.config/containers/systemd/`:

| File | Unit | Responsibility | Published ports | Limits as written |
|---|---|---|---|---|
| `pkb.network` | `pkb-network` | the single container network | — | — |
| `pkb-postgres.container` | `pkb-postgres.service` | canonical PostgreSQL + separate Keycloak database | none (network-internal) | `--memory=768m`, `--cpus=1`, pids 256, `MemoryMax=768M` |
| `pkb-keycloak.container` | `pkb-keycloak.service` | identity realm; JVM ships inside the official image | `127.0.0.1:19281:8080`, `127.0.0.1:19284:9000` | `--memory=2048m`, `--cpus=1.5`, pids 512, `MemoryMax=2048M` |
| `pkb-openviking.container` | `pkb-openviking.service` | vector projection | `127.0.0.1:19285:1933` | `--memory=2g`, `--cpus=1.5`, pids 512 |

Copy exactly these four files, then `systemctl --user daemon-reload`. Use exactly this list. Historical unnamed templates are under deploy/quadlet,
not this directory; do not install them.

All seven permanent services autostart through `WantedBy=default.target`
(`pkb-postgres`, `pkb-keycloak`, `pkb-openviking` here; the four product units in
[deploy/quadlet](../quadlet/README.md)). A parent/host memory ceiling is managed
separately and is not asserted by these units.

## Prerequisites

The closed environment/config paths under `%h/ai-wisdom-config/`
(`postgres.env`, `keycloak.env`, `openviking/ov.conf`, …) must already exist and
must remain the only location of secrets; no values appear in this repository.
These files are templates, **not** an automatic secure first installation —
provisioning the config is a separate owner step documented in
[docs/runbooks/runtime.md](../../docs/runbooks/runtime.md). Upgrades perform no
setup and include no old-database discovery or cleanup.

See also [deploy/quadlet](../quadlet/README.md) and
[deploy/maintenance](../maintenance/README.md).