# Tested xorek infrastructure base

Current target: xorek 150.241.115.134, rootless user kb-inspect (UID1003),
Podman 4.9.3, systemd Quadlet. Parent slice: MemoryMax 8 GiB,
MemoryHigh 7.5 GiB, CPUQuota 400%, TasksMax 2048.

The included network/PostgreSQL/Keycloak units were generated and started on
that host. Start and restart were checked with readiness, OIDC discovery and
persistent table inventory. Both official images are pinned by digest.
Keycloak includes its own JVM; no host Java installation is required.
PostgreSQL has no host port; Keycloak is bound only to 127.0.0.1:19281,
management readiness only to 127.0.0.1:19284.

Closed operator config lives under ~/ai-wisdom-config, data under
~/ai-wisdom-data. Secrets are NOT included here. Keycloak realm import is
rendered from deploy/identity with the chosen localhost URLs and a closed
client credential. Its import mount is group-readable to the official
UID 1000/GID 0 image; the host config parent remains 0700.

Install these units into ~/.config/containers/systemd after preparing the
closed environment/realm files and distinct PostgreSQL database/role for
Keycloak, then daemon-reload. WantedBy=default.target supplies autostart.
NetworkAlias is unsupported by 4.9.3; these units use PodmanArgs instead.
MemoryMax/CPUQuota belong to [Service], not [Container].

Only the available infrastructure is deployed. The current source tree lacks
Containerfiles and an assembled gateway; it cannot run the complete product.
Legacy deploy/quadlet units remain unverified delivery templates.

Embedding provider is deliberately unconfigured: see
../embeddings/provider.env.example. Ollama and semantic indexing/search are
not started. Do not use the old Qwen/1024 prototype defaults as an owner
choice. Runtime OpenViking SDK/secret-sink and live Keycloak/MCP tests still
need implementation before E03 acceptance.

MiniMax CLI 1.0.25 is installed on the server with an OAuth-only guarded
launcher ~/.local/bin/mmx-subscription. A fresh server-specific OAuth login
was completed on this host and independently verified with method=oauth,
Coding Plan quota and a real MiniMax-M3.1-Flash-Preview request. The first
delegated helper returned max_tokens with thinking only and no accepted
artifact. The product model-runner remains unassembled. New installations
need their own OAuth login; Mac/cloud-session OAuth is not copied.
No API/PAYG fallback was used; this proof does not establish a billing hardcap.
