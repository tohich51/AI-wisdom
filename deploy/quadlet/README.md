# Quadlet units — target runtime (E01)

These are Quadlet `.container` files. They are **delivery artifacts**: nothing
here has been started in the cloud sandbox, because it has no systemd. On a
rootless-Podman host, `systemctl --user daemon-reload` turns them into
`~/.config/systemd/user/*.service` units.

Naming follows the PRODUCT-SPEC list. Memory limits are the **roomier** profile
from `reference/capacity.json`, not the pilot one:

| unit | MemoryMax | notes |
|---|---|---|
| gateway | 384M | FastAPI + MCP SDK + static React-admin |
| postgres | 768M | authority for data and RLS |
| keycloak | 2048M | needs a JRE in the image; none exists yet |
| openviking | 1280M | needs an embedding endpoint; not provisioned yet |
| embeddings | 1536M | CPU only, 1024 dims |
| worker | 1024M | Procrastinate |
| model-runner | 256M | subscription-scoped, one call at a time |

Total 7 296 MiB, plus 256 MiB transient allowance for maintenance jobs.

**Secrets are never in these files.** The render step substitutes from the
environment; the units themselves carry only variable references.
