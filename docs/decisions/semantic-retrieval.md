# Semantic retrieval

OpenViking 0.5.0 is an internal, rebuildable projection. Its official Python SDK
0.1.14 uses the existing HTTP transport. OpenRouter runs
`qwen/qwen3-embedding-8b`, native dimension 4096; no local model/GPU is needed.
The immutable embedding profile includes model, dimension, query instruction,
chunking policy and processor version. Its SHA256 binds every account and generation.

PostgreSQL owns libraries, membership, publications, sources, locators and version
history. Only published knowledge is indexed; extraction still creates drafts for
review. Withdrawing a source excludes derived knowledge and advances its library's
content revision. Rules and experience retain their existing exact-version tools.

Each library has an isolated OpenViking account. The dedicated indexer holds the
root administration capability; it installs a restricted indexed `/index` root and
two USER identities: read and write. The gateway mounts only the read-key directory.
Neither gateway nor ordinary ingestion worker can provision or alter the index.

Procrastinate polls changes every minute and serializes builds. The indexer also
holds a PostgreSQL installation-wide lock. It verifies files, processing errors and
complete vector coverage through OpenViking's read-only consistency checker before
activation. PostgreSQL checks the content revision, profile, document manifest and
all canonical hashes atomically. Checkpoints survive bounded retries; terminal
failure retires the build. A narrowly scoped cancellation function can retire only
the caller's own unfinished build after revocation.

HTTP and MCP use one search facade. Vector results supply URIs and scores only.
Canonical text and provenance are hydrated from PostgreSQL after current membership,
library grants, account profile and generation are checked again. No upstream
snippet becomes an answer. Unavailable or pending libraries explicitly fall back to
lexical search; the response reports semantic, hybrid or lexical mode. Reads never
call the generative MiniMax runner or initiate indexing.

Operational boundaries: one OpenViking writer, no claimed HA; 20-library semantic
fan-out budget per request, with explicit lexical fallback for the remainder.
Restore must include OpenViking data/auth metadata and closed service keys alongside
PostgreSQL. Rebuilding vectors never makes old or differently profiled generations
current. Native MiniMax Cloud client registration remains a separate capability;
the server bridge uses its own scoped OAuth profile.
