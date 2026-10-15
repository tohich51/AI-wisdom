# Runtime on xorek

AI-wisdom is a fresh self-hosted install for an owner and invited colleagues. PostgreSQL is the authority for library ACL, immutable versions, provenance and experience. A single authenticated MCP endpoint shares the catalog with the browser UI.

## Services

Rootless Podman 4.9 + systemd Quadlet, user kb-inspect, 8 GiB slice. PostgreSQL 768 MiB; Keycloak 2048 MiB; gateway 512 MiB; document worker 1536 MiB; MiniMax runner 768 MiB. One worker and one generation call at a time. HTTPS terminates at the existing Caddy. Gateway binds host loopback19280; identity19281. No public database/model ports or container sockets.

## Materials and approval

Upload immutable PDF/EPUB/DOCX/HTML/text originals. Unknown author/edition remain empty. PDF file pages and printed page labels are distinct; EPUB chapter/paragraph and DOCX table/cell positions survive parsing. Scanned PDFs report needs_ocr. Extraction is an explicit owner action using the existing MiniMax OAuth subscription, exact MiniMax-M3.1-Flash-Preview; no API key/PAYG fallback. Returned knowledge and rules stay drafts until a curator approves. Each extraction chunk commits drafts and a checkpoint together. Up to30 calls per job; partial jobs can be continued without recreating committed drafts. Failed/truncated/invalid model answers do not become knowledge. An interrupted call before commit can be retried and consume another subscription call.

Published rule versions are immutable. Corrections create a new version. Retrieval is separate from confirmed application and reported outcome. Corrections to an outcome append history. Export yields SKILL.md, does not install or run it.

## Deferred by owner

Embedding provider is unconfigured. OpenViking indexing and semantic search remain off. PostgreSQL lexical search is available and does not call a model. OpenViking adapter configuration remains a rebuildable projection boundary; there is no fake semantic index.

## Operations

Build Linuxamd64 image from an exact local source commit, hashed requirements-runtime.lock and wheel. Keep runtime env outside the image under /home/kb-inspect/ai-wisdom-config (0600). Only the model runner mounts the existing host OAuth profile, allowing refresh without copying it. Never put credentials/book text/thinking in public receipts or Git.

Install migrations with kb migrate and kb.jobs.schema.install_queue as the one-shot database admin; gateway and worker use their separate NOSUPERUSER/NOBYPASSRLS login roles. Quadlets are templates; render __RELEASE_SHA__ into the image reference. Validate generated systemd units before activation. Preserve the prior infra Quadlets/env and Caddy config for rollback.

For encrypted removable backups, pause the gateway and worker writers, wait for generation to stop, export both PostgreSQL databases/roles plus configured persistent data/config, resume writers in finally, then copy directly into the verified APFS restic repository on the Mac. Credentials for the restic repository stay on the internal Mac. Restore only into a separate test PostgreSQL and verify ACL/version data before calling a backup usable. OAuth is never copied into an agent handoff/image.
