# Removable local backups

Use existing PostgreSQL pg_dump/pg_restore, SSH/rsync and restic.
The operator chooses a writable external DATA volume and pins its UUID and
exact mountpoint. A Time Machine APFS Backup volume is not a restic target.
An absent/mismatched/read-only volume must fail BEFORE a server export or
writing a fallback folder on the Mac.

1. Pause all project writers by their explicit release contract. The current
   infrastructure-only exporter refuses active gateway/worker units and
   pauses Keycloak. It exports the canonical and Keycloak databases, roles,
   closed project configuration and immutable originals with a SHA256 manifest.
2. Pull into a private 0700 staging directory; verify every byte/hash.
   Never copy live PGDATA or MiniMax OAuth profiles.
3. Create a restic snapshot on the selected removable volume. Keep the
   recovery key 0600 on the internal owner Mac, separately from that volume
   and from the server. Do not include key material in Git or logs.
4. Run restic check --read-data and restore the exact snapshot into a new
   private directory; verify the manifest again.
5. Restore both dumps and roles to a disposable PostgreSQL target with no
   network, host ports or outbound workers. Compare restored data/grants and
   disabled identities. Preserve the production database.
6. Rebuild the search projection after an embedding provider is selected.
   Original text and canonical extracted knowledge must not need repeated
   LLM generation.

Current verification: an encrypted restic snapshot on a separate removable
APFS DATA volume passed check --read-data and restore --verify. The restored
export manifest and all file hashes were verified before both infrastructure
DB dumps and roles were restored to an isolated PostgreSQL target without
network or host ports. Canonical DB has 0 application tables, Keycloak 87;
Keycloak role is neither superuser nor BYPASSRLS. Production databases were
not overwritten, and the temporary target was removed. This accepts the
infrastructure backup/restore roundtrip, not a complete product/ACL restore
gate. No schedule or retention deletion was created.

Snapshot: 915a18c9056823fc96c02e10017cab4a071e74d47e3c0a3dc73c76d0c9c8351d.
Export manifest SHA256:
12330594ed573e88c80419d3ca6a6ea179f7db0a001b73e807a2704eddf39a14.
Combined evidence: e04-combined-backup-restore-readback.json in the handoff.

APFS rounds its quota to an allocation block: the requested 50,000,000,000
bytes became 50,000,003,072 bytes with 4096-byte blocks. A volume-creation
helper must verify this rounded value and stop if the target already exists;
never repeat addVolume just because a post-creation check failed.

The operator commands and environment proof are returned in the E01-E04
handoff; storage.example.json intentionally contains no invented UUID.
