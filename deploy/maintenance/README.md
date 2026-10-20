# deploy/maintenance — provisioning one-shot and timer

Two units: the provisioning one-shot and its timer. They are *not* permanent
services; there is no `WantedBy=default.target` here.

## Install (explicit list — no wildcard)

| File | Destination | Unit |
|---|---|---|
| `pkb-provision-index.container` | `~/.config/containers/systemd/` | `pkb-provision-index.service` (`Type=oneshot`, `RemainAfterExit=no`) |
| `pkb-provision-index.timer` | `~/.config/systemd/user/` | `pkb-provision-index.timer` (`WantedBy=timers.target`) |

The `.container` file goes with the other quadlet units; the `.timer` goes to the
systemd user directory. Then `systemctl --user daemon-reload`.

## Behaviour and limits

- Runs `provision-index run` after `pkb-postgres` and `pkb-openviking`; the timer
  fires `OnActiveSec=15s` / `OnUnitInactiveSec=30s` to drain pending library
  provisioning only.
- It is **not** a second index writer: `pkb-indexer` remains the single writer of
  published knowledge for the one product generation.
- Limits as written: `MemoryMax=512M`, `CPUQuota=100%`, `TasksMax=256`,
  `PidsLimit=128`, `TimeoutStartSec=1800`. A parent/host memory ceiling is
  managed separately.
- The root key is read-only; service/bootstrap key directories are writable
  outputs for the provisioning process, under the closed `%h/ai-wisdom-config/` paths
  (`provisioner.env`, OpenViking root key, service keys, bootstrap keys); no
  secret values appear in these files or in this README. `Pull=never`.

The image is `localhost/ai-wisdom:__RELEASE_SHA__`; substitute the accepted SHA
**only** after an exact `linux/amd64` build has passed acceptance on that host.

## Historical files — do not install

`kb-provision-index.container` and the duplicate `pkb-indexer.container` in this
directory are historical. It is an older alternative for the same unit filename: use only the product
unit from deploy/quadlet. Both historical files are retained, not installed.

## Scope

These templates reflect the accepted current server (source `65a6`); a new-host
install is not verified here, and nothing here performs setup on upgrades or
cleans old databases. See [deploy/xorek](../xorek/README.md),
[deploy/quadlet](../quadlet/README.md) and
[docs/runbooks/runtime.md](../../docs/runbooks/runtime.md).
## Existing administrative commands

The same CLI supports list-requests, run (a bounded batch), run UUID (resume)
and show UUID. It requires kb_provisioner without SUPERUSER/BYPASSRLS and derives
actor/account from the canonical PostgreSQL request; keys never appear in argv.
The existing shared advisory lock serializes provisioning with index writes.
A stored key file allows a partial request to resume without issuing another key.
