# Removable backups and restore validation

## Current boundary
- Active embedding configuration is OpenRouter `qwen/qwen3-embedding-8b` at 4096 dimensions.
- The old `index_rebuild_pending_provider_choice: true` receipt field is stale and unconditional; provider choice is not blocked.
- The known database restore does not establish current-source full-product recovery. Full auth/UI/read/index restore remains not accepted.

## External destination and retention
- On the owner Mac, settings are in `~/.config/ai-wisdom/backups/storage.json`.
- The restic repository is `/Volumes/AI-wisdom Backups/AI-wisdom/restic`.
- The expected external DATA UUID is `C2D7E493-E0E7-457C-AC9B-C8DE78276EAB`.
- Before backup/export and repository writes, verify that UUID, the actual mount, external-media identity, and writable state.
- The `restic-password` key file must remain mode `0600` and physically separated from both the server and removable media.
- If validation fails, stop; never fall back to creating a directory, initializing a repository, or selecting another volume.
- After transfer and verification processes close, the device may be normally and safely unmounted. If it is absent, make no automatic daily-RPO promise.
- Preserve current and rollback receipts, canonical originals, and encrypted backup evidence; only completed temporary transfer copies may expire, and only after exact readback.

## Capture and restore validation
- Record the exact backup identifier, source SHA, OCI image, manifest/hash, and key availability; do not combine unrelated snapshot scopes.
- Run all `/home/kb-inspect` entry points as `kb-inspect` UID `1003`, with home `/home/kb-inspect`; do not guess environment settings.
- `/home/kb-inspect/.local/bin/ai-wisdom-runtime-backup-export`
- This is the accepted quiesced export contract for all project writers and Keycloak; it refuses active or unknown provider work.
- It writes `ai-wisdom-data/exports/<timestamp>/manifest.json`. Verify the exact directory with:
- `/home/kb-inspect/.local/bin/verify-ai-wisdom-export <exportdir>`
- Keep project recovery configuration only inside the existing private encrypted backup path; never expose its values in source, prompts, reports or logs.
- Exact source Git bundles and checksums are required recovery artifacts. OAuth profiles (`~/.mmx`, `~/.codex`), macOS Keychain and personal session material are outside project exports.
- Complete the existing external-repository restic verification before decryption; no environment-specific command is asserted here.
- Use the existing private `prepare-restore-inputs` crypto path with the verified repository, exact snapshot, and separated key; do not invent flags or environment variables.
- It creates `/home/kb-inspect/ai-wisdom-data/restore-inputs/<manifestSHA>` containing actually decrypted DB dumps and an independently decrypted, hash-equal archive.
- Run `/home/kb-inspect/.local/bin/ai-wisdom-runtime-restore-check <thatdir>`, where `<thatdir>` is that restore-input directory.
- It restores disposable PostgreSQL with no network or host ports and compares roles, data, schema, grants, RLS, disabled users, originals, migration hashes, queues, and exact source image.
- It never overwrites production and never automatically resumes outbound jobs.

## Required safe order
1. Select the exact backup and confirm its source, OCI image, hashes, and key availability.
2. Quarantine all restored queues before any possible resume.
3. Restore roles, database data/schema/grants/RLS, and immutable canonical originals.
4. Verify real role-catalog user reads and negative access by disabled identities.
5. Reconcile index accounts and backend projections.
6. Run an explicit single-writer rebuild from restored published canonical knowledge without LLM generation.
7. Verify search results and provenance.
8. Consciously resume outbound jobs only after all unknown queue states are reconciled.

Do not repeat book extraction, rewrite IDs, automatically generate content, create a permanent test stand, or start a new OpenViking process in this scope.

## C32 index-rebuild gap
- Deployed source `156389989a` has no explicit rebuild CLI: restored ready SQL generation can return unchanged before a missing backend projection is checked.
- The REC1 candidate adds `--rebuild-library <UUID>` to the existing indexer entrypoint. It queues one explicit rebuild with the existing writer lock and durable retry; the flag is not a claim of deployment or restore acceptance.
- Before acceptance, that operator workflow must prove actual reconciliation of restored accounts and backend projections despite an unchanged canonical generation.
- Proof must cover a missing projection, enforce one writer, show rebuild from published canonical knowledge without generation, and pass search and provenance checks.
- No operator `--force` command, ad hoc SQL update, existing-index assertion, or temporary DB inventory substitutes for that proof.
- Until implemented and proven, stop at the index step; full auth/UI/read/index recovery remains not accepted.

## Evidence status
Latest provider and source pointers are current operator receipts, not fixed historical snapshots. Existing source release/readback documents are dated snapshots and do not create an automatic global-ready claim.

| Evidence class | Receipt or snapshot | Established scope | Limit |
|---|---|---|---|
| Actual DB restore | `6dcc8dc5742250db98b8cbff0fdc01d8a81e12679789e448368d0321d7e522ff`, source `e664889a` | Consistent canonical PG, Keycloak, roles/config, and 25 originals; external-copy restore verified DB data, grants, RLS, disabled identities, and original hashes | Actual restore, not manifest-only; not current-source full-product recovery |
| Current-source only | Source `156389989a54e1c69414afef9ea2001791ea46ec`; snapshot `39adc150279b11bb7a5872a0e0eaf72a41ce7d981277f395b16b7de4336f1a46` | Current app source-only receipt | Different scope; not a full DB snapshot |
| Older cold component | `376e0ad1a2af4156dee3c01a8eac97aeda62ea392a4847e874744310a28f4a57` | Separate older OpenViking cold-component snapshot | Never assume its generations agree with newer SQL |
| Full product | Current source plus a matching full DB snapshot | Requires the complete safe order and C32 implementation/proof | Pending; auth/UI/read/index restore is not yet accepted |
