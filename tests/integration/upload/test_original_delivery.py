"""A1 — delivering an original: bounded, streamed, and finished with the database.

This is the card's own regression file, and every assertion here is one of the
four things the card names:

1. **composed ``/api/v1`` and ``/api``, canonical Location including a replay**
   -> ``test_both_prefixes_answer_one_operation_with_a_canonical_location``,
   ``test_a_replay_returns_the_same_canonical_location``,
   ``test_the_alias_does_not_run_a_post_twice``
2. **bounded delivery: no object-sized buffer** -> ``test_the_body_arrives_in_
   bounded_chunks``, and the object is three megabytes while the largest chunk
   the process is allowed to hold is a quarter of one
3. **no database connection held while the body streams** ->
   ``test_no_pooled_connection_is_held_while_the_body_streams``
4. **descriptor released when the client disappears** ->
   ``test_an_aborted_download_does_not_leave_a_descriptor_open``,
   ``test_a_dead_transport_releases_the_descriptor_with_the_response_alive``,
   ``test_a_dead_transport_before_the_first_byte_releases_the_descriptor``
5. **a bounded integrity prepass, and no re-verification per Range** ->
   ``test_a_damaged_object_is_refused_by_a_whole_read``,
   ``test_a_range_read_does_not_re_verify_the_whole_file``
6. **active MIME: attachment, nosniff, CSP, on 200 and on 206** ->
   ``test_an_active_media_type_is_an_attachment_on_200_and_206``,
   ``test_an_inert_media_type_stays_inline``
7. **the audit is committed before the first byte moves** ->
   ``test_the_retrieval_is_recorded_before_the_bytes_move``

The application under test is the *composed* one: ``kb.http.router.mount_api``
serves the same router object under ``/api/v1`` and under ``/api``, which is the
declaration the card's second check is about. ``tests/integration/upload/
conftest.py`` mounts the routers with no prefix at all, so the two-prefix
behaviour is unreachable from those fixtures and this file builds the composed
app itself. That is why no shared fixture was edited: the new guarantee needs an
app the shared fixtures do not have, and the card allows a new file.

Two of these tests cannot go through a client. ``TestClient`` collects the whole
body before returning, so "while the body streams" is not observable through it,
and a driver that watches one chunk land, checks the pool, and then hangs up is
what makes the pool and the descriptor claims real rather than inferred. That
driver is :func:`_drive`, a raw ASGI call — no mocks anywhere in this file, the
same real PostgreSQL and the same real filesystem as the rest of the suite.

Four more cannot, and for a sharper reason: a client that hangs up *politely* is
a cancellation, and a cancellation already reaches the body generator's
``finally``, so it cannot tell a release that works from one that only works when
the loop is shutting down. What is not a cancellation is a **transport that
fails** — uvicorn writing into a closed socket gets a broken pipe back, and the
``OSError`` comes back out of ``send`` with nothing cancelled anywhere.
:func:`_drive_with_a_dead_transport` drives exactly that, and both of its tests
measure the descriptor from *inside* the running event loop, while the response
is still reachable from the failure's traceback. Measured after ``asyncio.run``
returns, every suspended generator in the loop has already been finalised and the
assertion would pass whether or not the application released anything at all.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import pytest

pytestmark = pytest.mark.integration

AUTH_HEADER = "X-Test-Auth-Subject"

# A PDF is the ordinary case and a small one, so most tests stay quick.
BOOK = b"%PDF-1.7\n" + b"chapter one. " * 500
# An HTML page is a legitimate source in this product, and it is also the
# media type that executes. Same bytes, different declared type: the media type
# on the manifest is what the response is built from.
PAGE = b"<html><body><h1>a page someone submitted</h1></body></html>"

# Three megabytes: large enough that a whole-object buffer would be obvious and
# small enough to keep the suite quick. The chunk ceiling is a quarter of that.
BIG = b"%PDF-1.7\n" + b"a longer book. " * 220_000


# --------------------------------------------------------------------- the app


class _VerifiedIdentity:
    """Identity as a plain ASGI wrapper: the shape a real deployment has.

    ``@app.middleware("http")`` is a ``BaseHTTPMiddleware``. It runs the
    application in a *second task* and copies body chunks out through a memory
    stream, so the frame that awaited a response is not on the traceback of a
    failed write, and a failed write leaves the response in a task nobody is
    holding. The real gateway has no HTTP middleware in front of a route at all:
    ``install_auth`` is a dependency, and the only ASGI middleware in the stack
    is ``TrustedHostMiddleware``.

    So this wrapper sets the same ``request.state.principal`` — the scope's
    ``state`` dict *is* ``request.state`` — without putting a task group between
    the transport and the response. That matters for two tests and for no others,
    and it is the whole reason ``gateway_stack`` exists.
    """

    def __init__(self, app, account_id: UUID) -> None:
        self.app = app
        self.account_id = account_id

    async def __call__(self, scope, receive, send) -> None:
        from fastapi.responses import JSONResponse

        from kb.access.policy import Principal

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            name.decode().lower(): value.decode() for name, value in scope.get("headers", ())
        }
        subject = headers.get(AUTH_HEADER.lower())
        if subject is not None:
            try:
                resolved = uuid.UUID(subject)
            except ValueError:
                response = JSONResponse({"detail": "malformed subject"}, status_code=401)
                await response(scope, receive, send)
                return
            scope.setdefault("state", {})["principal"] = Principal(
                principal_id=resolved, account_id=self.account_id, generation_watermark=1
            )
        await self.app(scope, receive, send)


def _composed_app(pool, store, account_id: UUID, *, gateway_stack: bool = False):
    """The real application, mounted under both prefixes over the real pool.

    ``mount_api`` includes one router object under ``/api/v1`` and under
    ``/api``; that is the property under test, so the app is built the way the
    gateway builds it rather than assembled from a fixture.

    The authentication middleware is the same labelled stand-in the upload
    conftest uses: it accepts a well-formed UUID subject and refuses anything
    else. It is not authentication, and nothing in this card's read path reads
    identity from the request. ``gateway_stack=True`` swaps it for the
    equivalent plain-ASGI wrapper, which is closer to the real deployment and is
    what the two dead-transport tests need; see :class:`_VerifiedIdentity`.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from kb.access.policy import Principal
    from kb.http.router import mount_api
    from kb.runtime_identity import build_connection_factory

    app = FastAPI(title="a1-original-delivery-under-test")
    app.state.db_connection = build_connection_factory(pool)
    app.state.blob_store = store

    if gateway_stack:
        app.add_middleware(_VerifiedIdentity, account_id=account_id)
    else:

        @app.middleware("http")
        async def stand_in_for_keycloak(request: Request, call_next):
            subject = request.headers.get(AUTH_HEADER)
            if subject is not None:
                try:
                    resolved = uuid.UUID(subject)
                except ValueError:
                    return JSONResponse({"detail": "malformed subject"}, status_code=401)
                request.state.principal = Principal(
                    principal_id=resolved, account_id=account_id, generation_watermark=1
                )
            return await call_next(request)

    mount_api(app)
    return app


class Client:
    """A real ASGI client over the composed app. ``as_`` re-authenticates."""

    def __init__(self, app, principal: UUID | None = None) -> None:
        from fastapi.testclient import TestClient

        self.app = app
        self.principal = principal
        self._client = TestClient(app)

    def as_(self, principal: UUID | None) -> Client:
        return Client(self.app, principal)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        merged = dict(extra or {})
        if self.principal is not None:
            merged.setdefault(AUTH_HEADER, str(self.principal))
        return merged

    def get(self, url: str, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.get(url, **kw)

    def post(self, url: str, **kw):
        kw["headers"] = self._headers(kw.get("headers"))
        return self._client.post(url, **kw)


@pytest.fixture
def composed(db_pool, store, world) -> Client:
    """A client on the composed application, over the real pool and store."""
    return Client(_composed_app(db_pool, store, world.organisation()))


@pytest.fixture
def gateway_stack(db_pool, store, world) -> Client:
    """The same application with no ``BaseHTTPMiddleware`` in front of a route.

    The same pool, the same store, the same composed app, the same identity —
    and a stack a real deployment would recognise. The difference only shows up
    when the transport fails, which is exactly when the two dead-transport tests
    need to be able to say "the response is still alive".
    """
    return Client(_composed_app(db_pool, store, world.organisation(), gateway_stack=True))


def upload(
    client: Client,
    library_id: UUID,
    payload: bytes,
    key: str,
    *,
    prefix: str = "/api/v1",
    name: str = "book.pdf",
    kind: str = "application/pdf",
):
    return client.post(
        f"{prefix}/libraries/{library_id}/sources/upload",
        files={"file": (name, payload, kind)},
        data={"idempotency_key": key},
    )


# ------------------------------------------- the raw ASGI driver, for streams


def _scope(
    path: str,
    principal: UUID,
    extra: dict[str, str] | None = None,
    spec_version: str = "2.3",
) -> dict:
    """One request scope. ``spec_version`` selects how a failure is delivered.

    2.3 is the default because the other tests in this file are about a client
    that hangs up, and below 2.4 Starlette runs the body in a task group beside a
    disconnect listener, so the hang-up cancels the generator. 2.4 and later are
    what a current uvicorn advertises, and there a failed write is an
    ``OSError`` out of ``send`` with nothing cancelled.
    """
    headers = [(b"host", b"testserver"), (AUTH_HEADER.lower().encode(), str(principal).encode())]
    for name, value in (extra or {}).items():
        headers.append((name.lower().encode(), value.encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }


class Streamed:
    """What one driven request produced."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.chunks: list[bytes] = []
        self.complete = False
        self.disconnects = 0

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)


def _drive(
    app,
    path: str,
    principal: UUID,
    *,
    after_first_chunk: Callable[[], None] | None = None,
    hang_up: bool = False,
) -> Streamed:
    """Run one request against the ASGI app, stopping where the test says.

    ``after_first_chunk`` runs inside the request, between the first chunk being
    handed to the server and the second being read — that is the only moment at
    which "the body is streaming" is a fact rather than a hope. ``hang_up`` then
    delivers ``http.disconnect`` on the receive channel, which is what a browser
    that closed the tab does; ``disconnects`` counts how many of those the
    application actually saw, so a test can assert the client really left.
    """
    result = Streamed()

    async def run() -> None:
        disconnect_now = asyncio.Event()
        finished = asyncio.Event()
        calls = 0

        async def receive() -> dict:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            if hang_up:
                await disconnect_now.wait()
            else:
                await finished.wait()
            result.disconnects += 1
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                result.status = message["status"]
                result.headers = {
                    key.decode().lower(): value.decode() for key, value in message["headers"]
                }
                return
            if message["type"] != "http.response.body":  # pragma: no cover - no trailers here
                return
            chunk = message.get("body", b"")
            if chunk:
                result.chunks.append(chunk)
                if len(result.chunks) == 1:
                    if after_first_chunk is not None:
                        after_first_chunk()
                    if hang_up:
                        # The client goes away here, between the first chunk and
                        # the second. Nothing else in the app is told, so the
                        # only way it can learn is the receive channel.
                        disconnect_now.set()
            if not message.get("more_body", False):
                result.complete = True
                finished.set()

        await app(_scope(path, principal), receive, send)

    asyncio.run(run())
    return result


def _descriptors_under(root: Path) -> int:
    """How many open file descriptors this process holds inside ``root``.

    Read from ``/proc/self/fd``, so it counts descriptors and not Python
    objects: a reader that kept the file object alive but closed nothing would
    still be reported here, which is the leak these tests are about.
    """
    prefix = str(root.resolve())
    count = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target.startswith(prefix):
            count += 1
    return count


class DeadTransport:
    """What one request against a transport that failed produced.

    ``responses`` is the interesting field: the streaming responses still
    reachable from the failure that ended the request. They are collected before
    the loop is shut down and kept in the result, so a test can hold the
    response alive for as long as it needs to.
    """

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.sent = 0
        self.failed_on: int | None = None
        self.status: int | None = None
        self.chunks: list[bytes] = []
        self.responses: list[object] = []
        self.generator_open = False
        self.descriptors = 0


def _retained_responses(exc: Exception | None) -> list[object]:
    """The streaming responses still alive because of one exception.

    The frames that awaited a response are still on the traceback of a
    ``ClientDisconnect``, so a response the application has not finished with is
    genuinely reachable: its reader is alive, its ``weakref.finalize`` has not
    run, and nothing about garbage collection can be credited with releasing
    the descriptor. Finding one is what stops the descriptor assertions in this
    file from passing vacuously.
    """
    from starlette.responses import StreamingResponse

    found: list[object] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        pending.append(current.__cause__)
        pending.append(current.__context__)
        traceback = current.__traceback__
        while traceback is not None:
            for value in traceback.tb_frame.f_locals.values():
                if isinstance(value, StreamingResponse) and not any(value is f for f in found):
                    found.append(value)
            traceback = traceback.tb_next
    return found


def _drive_with_a_dead_transport(
    app,
    path: str,
    principal: UUID,
    store_root: Path,
    *,
    fail_on_send: int,
) -> DeadTransport:
    """Run one request whose server stops accepting writes after N messages.

    ``fail_on_send`` counts the messages that get through: 0 fails on the
    response start, before the body generator has been entered at all, and 1
    fails on the first body write, with a chunk already read from the
    descriptor. ``OSError`` from ``send`` is what a real server raises when the
    peer is gone.

    What comes back out is deliberately not asserted on, because it is not the
    application's to choose: Starlette re-raises a failed write as
    ``ClientDisconnect`` from the frame that saw the ``OSError``, and a
    ``BaseHTTPMiddleware`` in the stack lets the raw ``OSError`` out instead. A
    release that only happens for one of those is not a release.

    Every measurement happens inside the coroutine, before ``asyncio.run`` gets
    a chance to shut the loop down. That shutdown calls
    ``loop.shutdown_asyncgens()``, which closes every suspended async generator
    in the loop — including the body generator of the very response under test —
    and a closed generator runs its ``finally``. Measured from the test body
    after ``asyncio.run`` returns, the descriptor is closed and the generator has
    finished *whether or not the application released anything*, so the
    assertion would pass against the unfixed code. That was measured, not
    assumed: both facts below are read while the loop is still running.
    """
    result = DeadTransport()

    async def run() -> None:
        async def receive() -> dict:
            # The client is not going to say anything, and from ASGI 2.4 on
            # nothing asks it to.
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            result.sent += 1
            if result.sent > fail_on_send:
                result.failed_on = result.sent
                raise OSError(32, "Broken pipe")
            if message["type"] == "http.response.start":
                result.status = message["status"]
                return
            if message["type"] == "http.response.body":  # pragma: no cover - no trailers
                result.chunks.append(message.get("body", b""))

        caught: list[Exception] = []
        try:
            await app(_scope(path, principal, spec_version="2.4"), receive, send)
        except Exception as exc:  # the ClientDisconnect the ASGI spec names
            caught.append(exc)

        # Held on purpose. This exception's traceback holds the frame that
        # awaited the response, and the response holds the reader, so the
        # descriptor's only possible release so far is one the response made
        # itself.
        result.error = caught[0] if caught else None
        result.responses = _retained_responses(caught[0] if caught else None)
        await asyncio.sleep(0)  # one full event-loop checkpoint
        # Read here, not in the test body. asyncio.run() closes every suspended
        # async generator in the loop before it returns, so a generator that
        # was never finalised looks finished by the time the test can look at
        # it, and an assertion made from there is measuring the shutdown.
        result.generator_open = any(
            response.body_iterator.ag_frame is not None for response in result.responses
        )
        result.descriptors = _descriptors_under(store_root)

    asyncio.run(run())
    return result


# =========================================== composed prefixes, canonical Location


@pytest.mark.parametrize("prefix", ["/api/v1", "/api"])
def test_both_prefixes_answer_one_operation_with_a_canonical_location(
    composed: Client, people, world, key: str, prefix: str
) -> None:
    """One operation, two prefixes, one canonical Location.

    The alias exists so the browser keeps working; it is not a second
    implementation of the upload, and the value a client is handed is the
    canonical path whichever prefix it posted to. ``/api/v1`` is served here, so
    the Location is followed and the bytes come back — an emitted path that
    404s is exactly the failure this assertion exists to catch.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    response = upload(client, library, BOOK, key, prefix=prefix)
    assert response.status_code == 201, response.text
    body = response.json()
    location = response.headers["location"]
    assert location == f"/api/v1/sources/{body['source']['id']}/original"
    assert location.startswith("/api/v1/")

    served = client.get(location)
    assert served.status_code == 200
    assert served.content == BOOK
    # The alias serves the same read, so a client that kept the old prefix is
    # not left with a dead link.
    assert client.get(location.replace("/api/v1", "/api", 1)).content == BOOK


def test_a_replay_returns_the_same_canonical_location(
    composed: Client, people, world, key: str
) -> None:
    """The retried submission is answered from the first one, and says where.

    A client that timed out resends the body and asks "did I upload twice". It
    did not, and both answers — the first and the replay — point at the same
    canonical read route for the same source.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    first = upload(client, library, BOOK, key)
    second = upload(client, library, BOOK, key)
    assert (first.status_code, second.status_code) == (201, 200), (first.text, second.text)
    assert first.json()["status"] == "stored"
    assert second.json()["status"] == "replayed"

    source_id = first.json()["source"]["id"]
    assert second.headers["location"] == f"/api/v1/sources/{source_id}/original"
    assert second.json()["source"]["id"] == source_id
    assert len(world.sources_in(library)) == 1


def test_the_alias_does_not_run_a_post_twice(composed: Client, people, world, key: str) -> None:
    """A POST through ``/api`` is executed once.

    Both prefixes mount one router object, so the effect happens once. The rows
    are the proof: a second execution would be a second attempt row or a second
    source, and there is exactly one of each.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = upload(composed.as_(people.owner), library, BOOK, key)
    assert response.status_code == 201, response.text
    assert len(world.sources_in(library)) == 1
    assert len(world.manifests_for(library)) == 1
    assert [row[0] for row in world.ingests_in(library)] == ["committed"]


# ================================================== active MIME: attachment etc.


def test_an_active_media_type_is_an_attachment_on_200_and_206(
    composed: Client, people, world, key: str
) -> None:
    """A submitted HTML page never becomes a document in this origin.

    Three headers, all on both answers: the bytes are an attachment, so there is
    nothing for the browser to render as a page; ``nosniff``, so an unlabelled
    body cannot be sniffed into one; and a sandbox CSP behind them, in case a
    client ignores the disposition. The media type is the one on the manifest,
    which came from the submitted bytes and the declared type.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    response = upload(client, library, PAGE, key, name="page.html", kind="text/html")
    assert response.status_code == 201, response.text
    assert response.json()["original"]["media_type"] == "text/html"
    source_id = response.json()["source"]["id"]

    whole = client.get(f"/api/v1/sources/{source_id}/original")
    assert whole.status_code == 200
    assert whole.content == PAGE
    assert whole.headers["content-disposition"] == "attachment"
    assert whole.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in whole.headers["content-security-policy"]

    part = client.get(f"/api/v1/sources/{source_id}/original", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206
    assert part.content == PAGE[:10]
    assert part.headers["content-disposition"] == "attachment"
    assert part.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in part.headers["content-security-policy"]
    assert part.headers["content-range"] == f"bytes 0-9/{len(PAGE)}"
    assert part.headers["content-length"] == "10"


def test_an_inert_media_type_stays_inline(composed: Client, people, world, key: str) -> None:
    """A PDF is served as a PDF.

    The hardening is not a blanket denial: an inert type keeps its inline
    disposition, because a catalogue whose originals all download is a
    catalogue nobody reads. ``nosniff`` is kept, because it costs an inert
    nothing and is the only thing that stops a browser deciding on its own.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    source_id = upload(client, library, BOOK, key).json()["source"]["id"]
    response = client.get(f"/api/v1/sources/{source_id}/original")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "content-disposition" not in response.headers
    assert "content-security-policy" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


def test_the_retrieval_is_recorded_before_the_bytes_move(
    composed: Client, people, world, key: str
) -> None:
    """The audit row exists as soon as the read is answered.

    Read as the migration role, on a separate connection, which is the only way
    to see that the write was committed rather than merely staged on a
    connection that was still open when the body started.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    source_id = upload(client, library, BOOK, key).json()["source"]["id"]
    assert client.get(f"/api/v1/sources/{source_id}/original").status_code == 200
    assert (
        world.count("SELECT count(*) FROM kb.retrieval_event WHERE item_id = %s", (source_id,)) == 1
    )


# ============================================== bounded, streamed, and no lease


def test_the_body_arrives_in_bounded_chunks(composed: Client, people, world, key: str) -> None:
    """Three megabytes in, and never more than a quarter of one held at once.

    A whole-object buffer would be invisible in the total and obvious in the
    parts: the response would be one body message of three megabytes. What the
    delivery promises instead is a sequence of bounded pieces, and that is what
    the client sees.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    source_id = upload(client, library, BIG, key, name="big.pdf").json()["source"]["id"]

    path = f"/api/v1/sources/{source_id}/original"
    streamed = _drive(client.app, path, people.owner)
    assert streamed.status == 200
    assert streamed.body == BIG
    assert len(streamed.chunks) > 1, "the whole object was sent as one piece"
    assert max(len(chunk) for chunk in streamed.chunks) <= 256 * 1024
    assert streamed.headers["content-length"] == str(len(BIG))


def test_no_pooled_connection_is_held_while_the_body_streams(
    composed: Client, db_pool, people, world, key: str
) -> None:
    """The stream costs no database connection.

    The pool holds four. With the lease released before the response exists,
    all four are still available in the middle of a download, and a fifth
    concurrent request for anything at all would not be waiting on a book. The
    check runs between the first and the second chunk: the body is mid-flight at
    exactly that point.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    source_id = upload(client, library, BIG, key, name="big.pdf").json()["source"]["id"]

    leases: list = []
    with contextlib.ExitStack() as held:

        def check_the_pool() -> None:
            for _ in range(4):
                leases.append(held.enter_context(db_pool.connection(timeout=5)))

        streamed = _drive(
            client.app,
            f"/api/v1/sources/{source_id}/original",
            people.owner,
            after_first_chunk=check_the_pool,
        )
    assert streamed.status == 200
    assert streamed.body == BIG
    assert len(leases) == 4, "the whole pool was free while the body streamed"


def test_an_aborted_download_does_not_leave_a_descriptor_open(
    composed: Client, store, people, world, key: str
) -> None:
    """A client that disappears halfway leaves no descriptor behind.

    The request hangs up after the first chunk, so the application is told the
    reader is gone while the body is still in flight. The descriptor is released
    anyway: a reader that only closes on the happy path is a file-descriptor
    leak, and a handful of abandoned downloads on a busy service is a full
    volume.

    How it is released is worth being exact about, because "cancelled" is not
    one thing. ASGI hands the response either to a task that can be cancelled
    or to a server that stops reading, and in the second case no ``close()`` is
    ever called. The release therefore does not depend on the server being
    polite: the stream closes its own reader in a ``finally``, and the reader
    releases its descriptor when it is collected, which is the moment the
    abandoned response goes away.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    source_id = upload(client, library, BIG, key, name="big.pdf").json()["source"]["id"]

    before = _descriptors_under(store.root)
    streamed = _drive(
        client.app, f"/api/v1/sources/{source_id}/original", people.owner, hang_up=True
    )
    assert streamed.status == 200
    assert streamed.chunks, "nothing was delivered before the hang-up"
    assert streamed.disconnects == 1, "the application never saw the client leave"
    assert _descriptors_under(store.root) == before


def test_a_dead_transport_releases_the_descriptor_with_the_response_alive(
    gateway_stack: Client, store, people, world, key: str
) -> None:
    """A broken pipe mid-body costs no descriptor, while the response is alive.

    This is the case a hang-up cannot reach. The server writes the first chunk
    and the next write fails, so the ``OSError`` leaves the response with the
    body generator still suspended at its ``yield`` — its ``finally`` has not
    run, and from ASGI 2.4 on there is no task group to cancel it and nothing
    that closes an abandoned iterator. The release belongs to the response, and
    it has to have happened by the time the failure reaches the application.

    So the measurement is taken from inside the running loop, with the failure
    — and therefore the frame that awaited the response — still held, and the
    response still reachable from it. A descriptor that waited for the response
    to be collected, or for ``asyncio.run`` to finalise the body generator, would
    still be open at exactly this point.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = gateway_stack.as_(people.owner)
    source_id = upload(client, library, BIG, key, name="big.pdf").json()["source"]["id"]

    before = _descriptors_under(store.root)
    result = _drive_with_a_dead_transport(
        client.app,
        f"/api/v1/sources/{source_id}/original",
        people.owner,
        store.root,
        fail_on_send=1,
    )

    assert result.error is not None, "a failed write did not fail the request"
    assert result.status == 200, "the response never started"
    assert result.failed_on == 2, "the failure did not land on the first body write"
    assert result.sent == 2, (
        "the response kept writing after the transport was gone, so this is "
        "not the mid-body failure under test"
    )
    assert result.responses, (
        "the response was not reachable from the traceback, so this test would "
        "say nothing about a response that is still referenced"
    )
    assert result.generator_open, (
        "the body generator had already been finalised inside the loop, so its "
        "own finally could have released the descriptor and the response need "
        "not have"
    )
    assert result.descriptors == before, "the descriptor outlived the failed write"


def test_a_dead_transport_before_the_first_byte_releases_the_descriptor(
    gateway_stack: Client, store, people, world, key: str
) -> None:
    """A transport that dies on the response start releases it just the same.

    The body generator is never entered here: the very first ``send`` fails, so
    there is no ``yield``, no suspension and no ``finally`` that could ever run
    — a generator that has not started has nothing to finalise. This is the
    half of the same guarantee that a generator-only release cannot cover at all,
    and the descriptor was already open before any of it: the route verifies
    the object against its content address while building the response.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = gateway_stack.as_(people.owner)
    source_id = upload(client, library, BOOK, key).json()["source"]["id"]

    before = _descriptors_under(store.root)
    result = _drive_with_a_dead_transport(
        client.app,
        f"/api/v1/sources/{source_id}/original",
        people.owner,
        store.root,
        fail_on_send=0,
    )

    assert result.error is not None, "a failed write did not fail the request"
    assert result.failed_on == 1, "the failure did not land on the response start"
    assert result.status is None, "the response started, so this is not that case"
    assert result.chunks == [], "the body was entered, so this is not that case"
    assert result.responses, (
        "the response was not reachable from the traceback, so this test would "
        "say nothing about a response that is still referenced"
    )
    assert result.generator_open, (
        "the body generator was finalised inside the loop, so this is not the "
        "case where it was never entered"
    )
    assert result.descriptors == before, "the descriptor outlived the failed start"


# ================================== the bounded prepass, and Range without one


def test_a_damaged_object_is_refused_by_a_whole_read(
    composed: Client, store, people, world, key: str
) -> None:
    """A whole read proves the bytes before it sends them.

    The object is damaged underneath the catalogue after it was stored — the
    volume drifted, a restore went wrong. The content-addressed key no longer
    describes what is there, so the answer is 503 and not a body: handing those
    bytes out under a manifest that claims otherwise would put a fabricated
    provenance into a reader's hands.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    body = upload(client, library, BOOK, key).json()
    assert "source" in body, body
    source_id = body["source"]["id"]

    path = store.root / body["source"]["object_key"]
    original = path.read_bytes()
    path.write_bytes(b"%PDF-1.7\n" + b"silently different. " * 40 + original[29:])

    response = client.get(f"/api/v1/sources/{source_id}/original")
    assert response.status_code == 503
    assert response.content != BOOK
    # A whole read that refuses must not leave the descriptor it opened.
    assert _descriptors_under(store.root) == 0


def test_a_range_read_does_not_re_verify_the_whole_file(
    composed: Client, store, people, world, key: str
) -> None:
    """A Range is a delivery of a part, and is not re-proved from the start.

    This is the contract's explicit trade-off, asserted in both directions. A
    whole delivery proves the object once, in one bounded pass. A Range does
    not: a PDF reader asks for a dozen slices, and re-hashing the book for each
    one is a full read per slice. So a byte damaged *outside* the requested
    range does not stop the requested bytes from being served — while the whole
    read over the same damaged object is refused, as the test above shows.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = composed.as_(people.owner)
    body = upload(client, library, BOOK, key).json()
    assert "source" in body, body
    source_id = body["source"]["id"]

    path = store.root / body["source"]["object_key"]
    damaged = bytearray(path.read_bytes())
    damaged[-1] = damaged[-1] ^ 0xFF  # the last byte: outside the range below
    path.write_bytes(bytes(damaged))

    response = client.get(f"/api/v1/sources/{source_id}/original", headers={"Range": "bytes=0-9"})
    assert response.status_code == 206
    assert response.content == BOOK[:10]
    assert response.headers["content-range"] == f"bytes 0-9/{len(BOOK)}"
