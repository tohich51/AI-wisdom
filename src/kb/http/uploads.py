"""C10 — HTTP surface for submitting and reading originals.

Three routes:

    POST /libraries/{library_id}/sources/upload   multipart file
    POST /libraries/{library_id}/sources/from-url a URL, under the SSRF policy
    GET  /sources/{source_id}/original             the bytes, or a byte range

**Where identity comes from.** ``request.state.principal``, set by the
authentication middleware after it has verified the token. This module reads no
header, no query parameter and no body field that can set it. ``SubmitFile`` and
``FetchUrl`` are ``extra="forbid"``, so a body carrying ``principal_id``,
``role`` or ``submitted_by`` is rejected with 422 before a handler runs — and
even if one were accepted, ``catalog.submit_file`` takes the principal as an
argument and never reads it from the spec.

**Where the bytes come from and go to.** ``app.state.blob_store`` is the one
handle on the object volume, configured at startup. No route takes a path, a
bucket or a directory from the request, and no response body contains one. A
request that wanted to name a server-side file has no field to do it in, which is
ARCHITECTURE.md §10's requirement that ``submit_source`` accept no arbitrary
server path.

**Where the URL transport comes from.** ``app.state.source_transport`` and
``app.state.source_resolver`` are process configuration. The gateway leaves them
unset and the real resolver and the real transport are used. A test sets them to
drive the route deterministically; a caller cannot, because nothing in the
request maps to ``app.state``.

**Range reads are authorised per request.** A ``Range: bytes=a-b`` header serves
a slice; the access check happens on every call and there is no signed URL,
because a signed URL would keep working after the grant is revoked
(SCALING.md §5).
"""

from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from kb.access.policy import AccessDenied
from kb.catalog import upload as catalog
from kb.catalog.storage import LocalBlobStore, ObjectNotFound, StorageError
from kb.catalog.upload_fetch import UrlRefused
from kb.http.libraries import Db, Me, mapped_errors

router = APIRouter(tags=["sources"])

# "bytes=0-1023", "bytes=1024-", "bytes=-512". A single range only: a multipart
# range is legal HTTP and buys a PDF reader nothing, so an unparseable or
# multi-range header is a 416 rather than a guess.
_RANGE_RE = re.compile(r"^bytes=(?P<start>\d*)-(?P<stop>\d*)$")

# The size ceiling advertised to a client. A deployment constant: the route
# never reads a limit out of the request, so a caller cannot raise its own.
MAX_UPLOAD_BYTES = catalog.DEFAULT_MAX_UPLOAD_BYTES

# Starlette renamed this constant and deprecated the old name. The number is
# fixed by RFC 9110, so it is written out rather than aliased to a name that
# moved.
HTTP_416 = 416


def blob_store(request: Request) -> LocalBlobStore:
    """The configured object store, or 503.

    Injected on ``app.state`` at startup by the gateway, not built by the route.
    A router that constructed its own store would put a filesystem path into
    application code, which is the thing the object-key contract exists to
    prevent.
    """
    store = getattr(request.app.state, "blob_store", None)
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the object store is not configured"
        )
    if not isinstance(store, LocalBlobStore):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the object store is misconfigured"
        )
    return store


def _upload_errors():
    """Map this card's refusals onto honest status codes.

    ``AccessDenied`` stays "forbidden" and never says whether the object exists.
    ``UrlRefused`` is the caller's URL being wrong, so 400 with the reason — a
    refused URL leaks nothing, because the caller supplied every part of it.
    ``StorageError`` is the volume's problem, not the caller's, so 503: telling a
    caller "bad request" for a full disk teaches operators to ignore 400s.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        with mapped_errors():
            try:
                yield
            except UrlRefused as exc:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.reason) from exc
            except StorageError as exc:
                raise HTTPException(
                    status.HTTP_503_SERVICE_UNAVAILABLE, "the original could not be stored"
                ) from exc

    return _ctx()


def _range_bounds(header: str | None, size: int) -> tuple[int, int] | None:
    """``(start, stop)`` with an exclusive stop, or None for a whole object."""
    if header is None:
        return None
    match = _RANGE_RE.match(header.strip())
    if match is None:
        raise HTTPException(HTTP_416, "unsupported Range")
    raw_start, raw_stop = match["start"], match["stop"]
    if raw_start == "" and raw_stop == "":
        raise HTTPException(HTTP_416, "unsupported Range")
    if raw_start == "":
        length = int(raw_stop)
        if length == 0:
            raise HTTPException(HTTP_416, "unsupported Range")
        return max(size - length, 0), size
    start = int(raw_start)
    if start >= size:
        raise HTTPException(HTTP_416, "the range starts past the end")
    stop = int(raw_stop) + 1 if raw_stop else size
    return start, min(stop, size)


def _location(source_id: UUID) -> str:
    """The read route for one original. A path, never a signed URL."""
    return f"/sources/{source_id}/original"


def _created_response(response: Response, result: catalog.SubmittedSource) -> None:
    """Set the status code and the Location of a submission.

    201 for a first submission and 200 for a replay or a deduplicated one. Both
    are successes; only one of them created something, and the body's ``status``
    says which. The Location points at the *read route*, never at a signed or
    direct URL, so every later read re-checks the grant.
    """
    response.status_code = (
        status.HTTP_201_CREATED if result.status == "stored" else status.HTTP_200_OK
    )
    response.headers["Location"] = _location(result.source.id)


# --------------------------------------------------------------------- submit


@router.post(
    "/libraries/{library_id}/sources/upload",
    status_code=status.HTTP_201_CREATED,
)
def upload_source(
    library_id: UUID,
    conn: Db,
    me: Me,
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="the original, as bytes")],
    idempotency_key: Annotated[str, Form(min_length=8, max_length=200)],
    title: Annotated[str | None, Form(max_length=300)] = None,
) -> catalog.SubmittedSource:
    """Accept a file into the library's object store.

    The body is streamed straight into the store: never held in memory, never
    parsed, never interpreted. The response carries the content hash and the
    object key, and no path.

    ``Location`` points at the read route, not at a signed URL — every later read
    re-checks the grant, so revoking it closes the original for good.

    201 for a first submission and 200 for a replay or a deduplicated one. Both
    are successes; only one of them created something, and the body's ``status``
    says which.
    """
    store = blob_store(request)
    spec = catalog.SubmitFile(library_id=library_id, title=title, idempotency_key=idempotency_key)
    with _upload_errors():
        result = catalog.submit_file(
            conn,
            me,
            store,
            spec,
            file.file,
            declared_media_type=file.content_type,
            filename=file.filename,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    _created_response(response, result)
    return result


@router.post(
    "/libraries/{library_id}/sources/from-url",
    status_code=status.HTTP_201_CREATED,
)
def submit_from_url(
    library_id: UUID,
    body: catalog.FetchUrl,
    conn: Db,
    me: Me,
    request: Request,
    response: Response,
) -> catalog.SubmittedSource:
    """Accept a URL, under the SSRF policy.

    The body is :class:`~kb.catalog.upload.FetchUrl` rather than
    :class:`~kb.catalog.upload.SubmitUrl`: the library comes from the path, and a
    body that also carried one would be a second, caller-controlled answer to
    "which library", which is the sort of duplication that ends up writing to the
    wrong one.

    The URL is untrusted. A loopback, private, link-local or metadata address, a
    non-http(s) scheme, credentials in the URL, a redirect to any of the above,
    or too many hops is refused here — before a socket is opened and before a row
    exists. See :mod:`kb.catalog.upload_fetch`.
    """
    store = blob_store(request)
    spec = catalog.SubmitUrl(library_id=library_id, **body.model_dump())
    # The transport and resolver are process configuration, never request data:
    # the gateway leaves them unset in production, and a test sets them so this
    # route can be driven over a mock transport without a network. A caller
    # cannot reach them — there is no header, query parameter or body field that
    # maps to app.state.
    transport = getattr(request.app.state, "source_transport", None)
    resolver = getattr(request.app.state, "source_resolver", None)
    with _upload_errors():
        result = catalog.submit_url(
            conn,
            me,
            store,
            spec,
            transport=transport,
            resolver=resolver,
            max_bytes=MAX_UPLOAD_BYTES,
        )
    _created_response(response, result)
    return result


# ---------------------------------------------------------------------- read


@router.get("/sources/{source_id}/original")
def read_original(
    source_id: UUID,
    conn: Db,
    me: Me,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    """The original's bytes, or a Range of them, after the access check.

    404 for a source the caller may not open and for one that does not exist —
    the same answer, because the difference is a directory of invisible objects.
    A revoked grant closes this immediately: no URL keeps working.
    """
    store = blob_store(request)
    described = catalog.describe_original(conn, me, source_id)
    if described is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    size = described.original.byte_size
    bounds = _range_bounds(range_header, size)
    try:
        result = catalog.read_original(
            conn,
            me,
            store,
            source_id,
            start=bounds[0] if bounds else None,
            stop=bounds[1] if bounds else None,
        )
    except AccessDenied as exc:  # pragma: no cover - the check above already ran
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source") from exc
    except ObjectNotFound as exc:
        # The row and the blob have drifted apart. Refuse; do not serve a
        # substitute.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the original is unavailable"
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "the original is unavailable"
        ) from exc

    headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"{result.original.content_hash}"',
        # The bytes are addressed by content, so a cache may hold them as long as
        # it likes; a new grant never makes a wrong body correct.
        "Cache-Control": "private, max-age=3600",
    }
    if result.complete:
        return Response(
            content=result.content, media_type=result.original.media_type, headers=headers
        )
    start = bounds[0] if bounds else 0
    end = start + max(len(result.content) - 1, 0)
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(
        content=result.content,
        media_type=result.original.media_type,
        headers=headers,
        status_code=status.HTTP_206_PARTIAL_CONTENT,
    )


__all__ = ["MAX_UPLOAD_BYTES", "blob_store", "router"]
