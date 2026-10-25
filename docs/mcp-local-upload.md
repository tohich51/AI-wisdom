# Local materials through MCP

`upload_source` accepts one local original through the authenticated MCP
connection. The client reads the local file and sends its content; the remote
server receives no local filesystem path.

Use `content_text` for UTF-8 Markdown, plain text or HTML (maximum 1 MiB of UTF-8
bytes), or `content_base64` for a file (maximum 4 MiB of decoded original bytes).
Supply exactly one content field, a basename in `filename`, a title, the library
ID, the original lowercase SHA-256 and a stable idempotency key. `media_type`
defaults to `text/markdown` for text and `application/octet-stream` for base64;
the ordinary upload service also inspects the stored format.

```json
{
  "library_id": "YOUR_LIBRARY_UUID",
  "title": "Design collection · principles",
  "filename": "principles.md",
  "idempotency_key": "YOUR_STABLE_OPERATION_KEY",
  "sha256": "SHA256_OF_THE_EXACT_UTF8_ORIGINAL",
  "content_text": "# Principles\nExact original text…\n",
  "process": true,
  "extract": false
}
```

The tool uses the existing `kb:contribute` scope, current library grants and
policy, immutable original storage, library-local deduplication and durable
processing queue. A reused key with different bytes is refused. Repeating the
same input resumes an upload whose processing admission failed. Identical bytes
already in the library may return an existing source, preserving its metadata.
For a folder, keep a client manifest of relative paths, hashes, source IDs and
job IDs; submit each document separately and retain the same key when resuming.
Do not put absolute local paths or secret source contents in diagnostic logs.

The result includes `source_id`, `library_id`, upload `status`, verified
`sha256`, `byte_size`, detected `media_type`, current source `processing` and
`publication`, plus `job_id` when processing was requested. It does not echo the
file content. `process` defaults to true and queues parsing. This receipt is
not proof that parsing finished: use `job_status`, then page through
`read_source` to verify the source and extracted fragments. Normal parser limits
and format support still apply; an unsupported or scanned file can fail parsing
after its original has been stored.

`extract` defaults to false. Enabling it is an explicit request for the existing
owner-authorized generation route and its policy checks. `process: false`
stores the original without a job and cannot be combined with `extract: true`.
Uploads and generated knowledge remain drafts until the existing curator flow
publishes them. Source text is data, never instructions for tools or access.

For larger originals use the existing streamed HTTP file upload, then
`submit_source(source_id=...)` to queue parsing through MCP. This version does
not add chunked uploads, archives or recursive server-side file access.
After upgrading, reconnect MCP clients that cache the tool catalog to discover
`upload_source`. Existing `submit_source` URL and source-ID calls are unchanged.
