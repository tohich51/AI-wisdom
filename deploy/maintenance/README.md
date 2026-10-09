# C15 — provisioning deployment kit (one-shot, rootless)

This directory is the **delivery artifact** for the provisioning operation: the
Quadlet unit that runs it, the volume its keys land in, and an environment
template. It is not a deployment that has been started. There is no Podman and
no systemd in the C15 workspace (`docs/handoff/runtime-capabilities.json`), so
the unit below has never been run, and `docs/handoff/results/C15.json` records
it as `not_run` rather than as a passing check.

## What this is

`kb-provision-index` is a **one-shot administrative task**. The application
*requests* provisioning (`kb.provisioning_request`); this unit carries it out
with the OpenViking root key. It is not a service, it is not restarted, and it
does not consume the worker queue.

```
gateway (kb_app)      --INSERT-->  kb.provisioning_request
                                             |
                                             |  read by the one-shot only
                                             v
one-shot (kb_provisioner, root key)  --POST--> OpenViking admin API
                        |
                        +--INSERT--> kb.index_account, kb.index_credential_ref
                        +--write---> kb-secrets/ volume (references only in PG)
```

The gateway never receives the root key, never holds the index identity, and
has no `INSERT` privilege on `kb.index_account` at all. That is enforced three
times over — by privileges, by an RLS policy that admits only the one-shot
role, and by a policy clause that requires the transport principal to manage
the library. The key is not the authority; the grant is.

## Files

| File | Purpose |
|---|---|
| `kb-provision-index.container` | Quadlet unit. `Type=oneshot`, rootless, no published ports, no container socket. |
| `kb-provision-index.volume` | The closed secret store the issued keys are written into. |
| `kb-provision-index.env.example` | Environment template. Every value is a `__PLACEHOLDER__`; the filled file is untracked. |
| `verify-no-container-socket.sh` | Black-box check against a deployed host: nothing outside the allowed set answers, and no socket is reachable from the app. |

## Invariants the unit file encodes (and the test checks)

These are asserted against the file in
`tests/integration/provisioning/test_boundaries.py::test_the_quadlet_unit_mounts_no_container_socket`,
so the artifact and the claim cannot drift apart:

* **No container socket.** No `/run/podman/podman.sock`, no
  `/run/docker.sock`, no `DOCKER_HOST`. The application does not start
  containers; it talks HTTP to the index server, which is the only thing it
  needs.
* **No published ports.** The unit publishes nothing. A15 is satisfied by the
  reverse proxy knowing about exactly two loopback ports, and this unit is not
  one of them.
* **Not a boot step.** No `[Install]` `WantedBy` — provisioning is an
  explicit `systemctl --user start kb-provision-index.service`, never
  something that happens because the host rebooted.
* **Read-only root, no capabilities, no new privileges.** The only writable
  path is the secret-store volume.

## Running it (on the target host)

```bash
cp deploy/maintenance/kb-provision-index.env.example /etc/kb/kb-provision-index.env
# fill the placeholders; the DSN is the kb_provisioner role, NOT postgres
chmod 0400 /etc/kb/kb-provision-index.env && chown root:root /etc/kb/kb-provision-index.env
install -m 0644 deploy/maintenance/kb-provision-index.* ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start kb-provision-index.service
```

The environment file is where the root key lives. It is `0400 root:root`
because the alternative — a key on a command line — is visible to every
process on the host in `ps`.

## What is NOT verified

* **The unit has never been started.** No Podman, no systemd (E01). Its
  syntax and its restrictions are checked by reading the file; its runtime
  behaviour is `not_run`.
* **There is no OpenViking admin client.** `kb.retrieval.provisioning` defines
  the boundary (`IndexAdminPort`) and refuses; `provisioning_cli` exits 3 with
  that reason. Writing a client for an API that cannot be called would be
  inventing a protocol, so the honest state of the remote half is
  `not_run` behind E03.
* **The key mount inside the container** depends on the target host's Podman
  version (`Secret=` support differs across 4.x/5.x). The unit uses an
  `EnvironmentFile`, which is the boring mechanism that works everywhere; if
  the deployment prefers Podman secrets, that is a one-line change to review
  with whoever owns `deploy/`.
