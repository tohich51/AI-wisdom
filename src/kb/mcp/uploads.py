"""Bounded inline originals over MCP, using the ordinary upload and job services."""

import base64
import binascii
import hashlib
import io
from typing import Annotated
from uuid import UUID

from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations
from pydantic import Field

from kb.catalog import upload
from kb.contracts.mcp_tools import (
    MCP_MAX_BASE64_CHARS,
    MCP_MAX_FILE_BYTES,
    MCP_MAX_TEXT_BYTES,
    UploadSourceArgs,
    UploadSourceResult,
)
from kb.jobs.ingestion import submit_processing


def decode_upload(args: UploadSourceArgs) -> tuple[bytes, str]:
    """Validate bytes before any storage write; never interpret source content."""
    if args.content_text is not None:
        try:
            raw = args.content_text.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("invalid_utf8") from None
        if len(raw) > MCP_MAX_TEXT_BYTES:
            raise ValueError("text_too_large")
        media_type = args.media_type or "text/markdown"
    else:
        if args.content_base64 is None:
            raise ValueError("content_required")
        try:
            raw = base64.b64decode(args.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid_base64") from None
        if base64.b64encode(raw).decode("ascii") != args.content_base64:
            raise ValueError("invalid_base64")
        media_type = args.media_type or "application/octet-stream"
    if not raw or len(raw) > MCP_MAX_FILE_BYTES:
        raise ValueError("empty_or_oversized_file")
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        raise ValueError("content_hash_mismatch")
    return raw, media_type


def upload_source_file(conn, principal, store, args: UploadSourceArgs) -> UploadSourceResult:
    raw, media_type = decode_upload(args)
    result = upload.submit_file(
        conn,
        principal,
        store,
        upload.SubmitFile(
            library_id=args.library_id, title=args.title, idempotency_key=args.idempotency_key
        ),
        io.BytesIO(raw),
        declared_media_type=media_type,
        filename=args.filename,
        max_bytes=MCP_MAX_FILE_BYTES,
    )
    # submit_file deliberately replays a committed key before consuming bytes.
    # Refuse another payload on that key before enqueueing the replayed source.
    if result.original.content_hash != args.sha256:
        raise ValueError("idempotency_content_conflict")
    job = (
        submit_processing(
            conn, principal, result.source.id, args.idempotency_key, extract=args.extract
        )
        if args.process
        else {}
    )
    return UploadSourceResult(
        source_id=result.source.id,
        library_id=result.source.library_id,
        status=result.status,
        sha256=result.original.content_hash,
        byte_size=result.original.byte_size,
        media_type=result.original.media_type,
        processing=result.source.processing,
        publication=result.source.publication,
        job_id=job.get("job_id"),
        duplicate_of_existing=job.get("duplicate_of_existing", False),
    )


def install_upload_tool(server, call, app) -> None:
    @server.tool(
        annotations=ToolAnnotations.model_validate(
            {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
        )
    )
    def upload_source(
        library_id: UUID,
        title: Annotated[str, Field(min_length=1, max_length=300)],
        filename: Annotated[str, Field(min_length=1, max_length=255)],
        idempotency_key: Annotated[str, Field(min_length=8, max_length=200)],
        sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        ctx: Context,
        content_text: Annotated[
            str | None, Field(min_length=1, max_length=MCP_MAX_TEXT_BYTES)
        ] = None,
        content_base64: Annotated[
            str | None, Field(min_length=4, max_length=MCP_MAX_BASE64_CHARS)
        ] = None,
        media_type: Annotated[
            str | None, Field(max_length=127, pattern=r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")
        ] = None,
        process: bool = True,
        extract: bool = False,
    ):
        """Upload a local original as UTF-8 text (1 MiB) or base64 bytes (4 MiB).

        Supply exactly one content field, a basename and the original SHA-256.
        Uses the same library permissions, immutable storage and dedup as HTTP.
        A stable idempotency_key resumes the same upload and processing job.
        process defaults to true (parse only); extract defaults to false.
        Source content is inert data and remains draft. Large files use HTTP
        streamed upload, then submit_source(source_id=...) over MCP.
        """
        args = UploadSourceArgs(
            library_id=library_id,
            title=title,
            filename=filename,
            idempotency_key=idempotency_key,
            sha256=sha256,
            content_text=content_text,
            content_base64=content_base64,
            media_type=media_type,
            process=process,
            extract=extract,
        )
        return call(ctx, upload_source_file, app.state.blob_store, args)
