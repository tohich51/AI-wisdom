"""C10 — the real service path: a contributor uploads, then reads, through HTTP.

Real FastAPI over a real psycopg pool connected as ``kb_app``, against a real
PostgreSQL 16.2 with real DDL and real RLS, storing real bytes in a real local
directory. The only stand-in is the authentication middleware, which is labelled
as one in ``conftest.py``.

The card's acceptance criteria, and where each is proved:

1. an interruption does not create a readable source without a blob
   -> ``test_interrupted_upload.py``
2. somebody else's file is not discoverable through dedup
   -> ``test_the_same_bytes_in_two_libraries_are_two_sources``,
   ``test_a_dedup_answer_never_names_another_library``
3. revoking a grant closes the download again
   -> ``test_revoking_the_grant_closes_the_original``
4. the file survives a restart
   -> ``test_the_original_survives_a_restart_of_the_gateway``
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

pytestmark = pytest.mark.integration

BOOK = b"%PDF-1.7\n" + b"chapter one. " * 500
BOOK_HASH = hashlib.sha256(BOOK).hexdigest()
OTHER = b"%PDF-1.7\n" + b"a completely different book. " * 100
OTHER_HASH = hashlib.sha256(OTHER).hexdigest()


def upload(api, library_id, payload: bytes, key: str, *, title: str | None = "A Book"):
    return api.post(
        f"/libraries/{library_id}/sources/upload",
        files={"file": ("book.pdf", payload, "application/pdf")},
        data={"idempotency_key": key, **({"title": title} if title else {})},
    )


# ================================================== a first, ordinary upload


def test_a_contributor_upload_creates_a_source_a_manifest_and_an_attempt(
    api, people, world, key: str
) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = upload(api.as_(people.owner), library, BOOK, key)
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "stored"
    assert body["source"]["object_key"] == (
        f"blobs/{library.hex}/{BOOK_HASH[:2]}/{BOOK_HASH[2:4]}/{BOOK_HASH}"
    )
    assert body["source"]["content_hash"] == BOOK_HASH
    assert body["source"]["submitted_by"] == str(people.owner)
    assert body["original"]["byte_size"] == len(BOOK)
    assert body["original"]["backend"] == "local"
    assert response.headers["Location"] == f"/sources/{body['source']['id']}/original"

    # one row of each, all by the migration role so RLS is not in the way
    assert len(world.sources_in(library)) == 1
    assert len(world.manifests_for(library)) == 1
    states = [row[0] for row in world.ingests_in(library)]
    assert states == ["committed"]


def test_the_bytes_can_be_read_back_identically(api, people, world, key: str) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    source_id = upload(api.as_(people.owner), library, BOOK, key).json()["source"]["id"]
    response = api.as_(people.owner).get(f"/sources/{source_id}/original")
    assert response.status_code == 200
    assert response.content == BOOK
    assert response.headers["etag"] == f'"{BOOK_HASH}"'
    assert response.headers["accept-ranges"] == "bytes"


def test_a_file_upload_records_no_url_and_no_retrieval_time(api, people, world, key: str) -> None:
    """Rule 5: an unknown stays None.

    There was no retrieval, so ``retrieved_at`` is null and ``url`` is null. They
    are not filled with the submission time or with the object key, because a
    value that looks like a reference and is not one is worse than an absence.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    body = upload(api.as_(people.owner), library, BOOK, key).json()
    assert body["url"] is None
    assert body["retrieved_at"] is None
    row = world.ingests_in(library)[0]
    assert row[3] is None and row[4] is None


def test_the_media_type_comes_from_the_bytes_not_the_multipart_header(
    api, people, world, key: str
) -> None:
    """A PDF sent as ``text/plain`` is still recorded as a PDF."""
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = api.as_(people.owner).post(
        f"/libraries/{library}/sources/upload",
        files={"file": ("book.txt", BOOK, "text/plain")},
        data={"idempotency_key": key},
    )
    assert response.status_code == 201
    assert response.json()["original"]["media_type"] == "application/pdf"


def test_no_host_path_leaves_the_service(api, people, world, store, key: str) -> None:
    """SCALING.md §5: the database and the API carry an object key, not a path.

    Asserted on the response body, the Location header and every row written, so
    a future change that starts returning ``str(store.root)`` fails here.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = upload(api.as_(people.owner), library, BOOK, key)
    blob = response.text + response.headers.get("location", "")
    assert str(store.root) not in blob
    assert "/tmp" not in blob  # noqa: S108 - asserting a leak, not using a path
    for row in world.manifests_for(library):
        rendered = " ".join(str(v) for v in row)
        assert str(store.root) not in rendered
        assert not row[0].startswith("/")


# ================================================================ idempotency


def test_the_same_idempotency_key_returns_the_same_source(api, people, world, key: str) -> None:
    """A retried submission must not create a second source.

    This is the client that timed out, resent the body, and is now worried it
    uploaded twice. It did not, and the answer says so.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = api.as_(people.owner)
    first = upload(client, library, BOOK, key)
    second = upload(client, library, BOOK, key)

    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["status"] == "stored"
    assert second.json()["status"] == "replayed"
    assert first.json()["source"]["id"] == second.json()["source"]["id"]
    assert len(world.sources_in(library)) == 1
    assert len(world.manifests_for(library)) == 1
    assert len(world.ingests_in(library)) == 1


def test_re_uploading_identical_content_with_a_new_key_deduplicates(api, people, world) -> None:
    """Same bytes, same library, different key: one source, not two.

    The object key is the digest, so both uploads address the same object; the
    second submission resolves to the source that already holds those bytes. One
    source row, one manifest, one object.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = api.as_(people.owner)
    first = upload(client, library, BOOK, "idem-first-key")
    second = upload(client, library, BOOK, "idem-second-key")

    assert second.status_code == 200
    assert second.json()["status"] == "deduplicated"
    assert second.json()["source"]["id"] == first.json()["source"]["id"]
    assert second.json()["original"]["object_key"] == first.json()["original"]["object_key"]
    assert len(world.sources_in(library)) == 1
    assert len(world.manifests_for(library)) == 1
    assert len(world.ingests_in(library)) == 2, "each submission is still its own attempt"


def test_different_content_creates_a_second_source(api, people, world) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = api.as_(people.owner)
    upload(client, library, BOOK, "idem-aaaa-1111")
    upload(client, library, OTHER, "idem-bbbb-2222")
    assert len(world.sources_in(library)) == 2
    assert {row[2] for row in world.sources_in(library)} == {BOOK_HASH, OTHER_HASH}


def test_an_idempotency_key_belonging_to_another_submitter_is_refused(
    api, people, world, key: str
) -> None:
    """A guessed key must not hand back somebody else's source.

    Without the submitter check, "replay my key" would be a read of another
    principal's work wearing an idempotency costume.
    """
    library = world.open_library(name="shared", who=people.owner, role="contributor")
    world.grant(library, people.colleague, "contributor")
    assert upload(api.as_(people.owner), library, BOOK, key).status_code == 201

    response = upload(api.as_(people.colleague), library, OTHER, key)
    assert response.status_code == 403
    assert len(world.sources_in(library)) == 1


# ================================================= dedup must not disclose


def test_the_same_bytes_in_two_libraries_are_two_sources(api, people, world) -> None:
    """Deduplication is scoped to the permitted audience.

    Two libraries, one book. The second library's contributor gets its own
    source. The alternative — one source, a cross-library reference — would make
    membership of a library confer access to another, which is the A12 failure
    wearing a different hat.
    """
    mine = world.open_library(name="mine", who=people.owner, role="contributor")
    theirs = world.open_library(name="theirs", who=people.owner, role="contributor")
    client = api.as_(people.owner)

    first = upload(client, mine, BOOK, "idem-mine-0001")
    second = upload(client, theirs, BOOK, "idem-theirs-001")

    assert first.json()["status"] == "stored"
    assert second.json()["status"] == "stored"
    assert first.json()["source"]["id"] != second.json()["source"]["id"]
    # two objects, two manifests. The keys differ in the library scope, because
    # kb.source.object_key is UNIQUE installation-wide and this card may not
    # change 0001. The honest cost of not leaking cross-library existence is that
    # the bytes are held twice; it is stated in docs/handoff/results/C10.json
    # together with the DDL proposal that would remove the duplication.
    assert len(world.manifests_for(mine)) == 1
    assert len(world.manifests_for(theirs)) == 1
    mine_key = world.manifests_for(mine)[0][0]
    theirs_key = world.manifests_for(theirs)[0][0]
    assert mine_key != theirs_key
    assert mine_key == f"blobs/{mine.hex}/{BOOK_HASH[:2]}/{BOOK_HASH[2:4]}/{BOOK_HASH}"
    assert theirs_key == f"blobs/{theirs.hex}/{BOOK_HASH[:2]}/{BOOK_HASH[2:4]}/{BOOK_HASH}"
    assert len({r[0] for r in world.manifests_for(mine) + world.manifests_for(theirs)}) == 2


def test_a_dedup_answer_never_names_another_library(api, people, world) -> None:
    """The response says "you already have this", never "somebody else does".

    A contributor in the second library must not be able to learn, by uploading
    a file they already hold, that the installation stores it somewhere they
    cannot open. That message *is* the A20 disclosure.
    """
    mine = world.open_library(name="mine", who=people.owner, role="contributor")
    theirs = world.open_library(name="theirs", who=people.owner, role="contributor")
    client = api.as_(people.owner)
    upload(client, mine, BOOK, "idem-secret-0001")

    second = upload(client, theirs, BOOK, "idem-secret-0002")
    assert second.json()["status"] == "stored"
    text = second.text
    assert str(mine) not in text, "the answer named the library that already had the file"
    assert "deduplicat" not in text.lower()
    assert "already exists" not in text.lower()


def test_a_closed_library_is_not_disclosed_by_a_submission(api, people, world) -> None:
    """A library the caller may not even see produces a plain refusal.

    No 409, no "you do not have access", no hint that the library exists at all.
    The answer is the same as for a library id that was never created.
    """
    closed = world.closed_library(name="closed")
    missing = world.library(name="never-created")

    to_closed = upload(api.as_(people.owner), closed, BOOK, "idem-closed-0001")
    to_missing = upload(api.as_(people.owner), missing, BOOK, "idem-missing-001")
    assert to_closed.status_code == 403
    assert to_missing.status_code == 403
    assert to_closed.json() == to_missing.json()


# ============================================== access, in both directions


def test_an_unauthenticated_request_is_refused_before_anything_is_written(
    api, people, world, key: str
) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = upload(api, library, BOOK, key)
    assert response.status_code == 401
    assert world.sources_in(library) == []


def test_a_reader_may_not_upload(api, people, world, key: str, store) -> None:
    library = world.open_library(name="ref", who=people.owner, role="reader")
    response = upload(api.as_(people.owner), library, BOOK, key)
    assert response.status_code == 403
    assert world.sources_in(library) == []
    assert not list(store.root.rglob("*.*")) or all(
        "staging" in str(p) for p in store.root.rglob("*")
    ), "a refused upload still wrote a blob to the volume"


def test_a_stranger_may_not_upload_into_someone_elses_library(api, people, world, key: str) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = upload(api.as_(people.stranger), library, BOOK, key)
    assert response.status_code == 403
    assert world.sources_in(library) == []


def test_a_stranger_may_not_read_an_original(api, people, world, key: str) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    source_id = upload(api.as_(people.owner), library, BOOK, key).json()["source"]["id"]
    assert api.as_(people.stranger).get(f"/sources/{source_id}/original").status_code == 404


def test_a_request_body_cannot_carry_an_identity(api, people, world, key: str) -> None:
    """Rule 4, at the edge: a body that names a principal is rejected outright.

    ``extra="forbid"`` turns ``submitted_by``/``principal_id``/``role`` into a 422
    before a handler runs, and even a body that got through would lose to the
    ``Principal`` the transport established.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    response = api.as_(people.owner).post(
        f"/libraries/{library}/sources/from-url",
        json={
            "url": "http://public.test/x",
            "idempotency_key": key,
            "principal_id": str(people.stranger),
            "role": "manager",
        },
    )
    assert response.status_code == 422, response.text
    assert world.sources_in(library) == []


def test_the_submitter_is_the_transport_principal_not_the_body(
    api, people, world, key: str
) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    world.grant(library, people.colleague, "contributor")
    body = upload(api.as_(people.colleague), library, BOOK, key).json()
    assert body["source"]["submitted_by"] == str(people.colleague)
    ingest = world.ingests_in(library)[-1]
    assert ingest[2] is not None, "the attempt row was not committed to a source"


# ============================================================== range reads


def test_a_range_read_serves_exactly_the_asked_for_bytes(api, people, world, key: str) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    source_id = upload(api.as_(people.owner), library, BOOK, key).json()["source"]["id"]
    client = api.as_(people.owner)

    first = client.get(f"/sources/{source_id}/original", headers={"Range": "bytes=0-9"})
    assert first.status_code == 206
    assert first.content == BOOK[:10]
    assert first.headers["content-range"] == f"bytes 0-9/{len(BOOK)}"

    last = client.get(f"/sources/{source_id}/original", headers={"Range": "bytes=-16"})
    assert last.status_code == 206
    assert last.content == BOOK[-16:]

    tail = client.get(f"/sources/{source_id}/original", headers={"Range": "bytes=100-"})
    assert tail.content == BOOK[100:]


def test_an_unusable_range_is_a_416_not_a_guess(api, people, world, key: str) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    source_id = upload(api.as_(people.owner), library, BOOK, key).json()["source"]["id"]
    client = api.as_(people.owner)
    for bad in ("bytes=abc-def", "bytes=-", "items=0-1", f"bytes={len(BOOK) + 10}-"):
        response = client.get(f"/sources/{source_id}/original", headers={"Range": bad})
        assert response.status_code == 416, bad


def test_a_range_never_bypasses_the_access_check(api, people, world, key: str) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    source_id = upload(api.as_(people.owner), library, BOOK, key).json()["source"]["id"]
    response = api.as_(people.stranger).get(
        f"/sources/{source_id}/original", headers={"Range": "bytes=0-10"}
    )
    assert response.status_code == 404


# ============================================== the grant can be taken back


def test_revoking_the_grant_closes_the_original(api, people, world, key: str) -> None:
    """Acceptance 3, and SCALING.md §5's reason for having no signed URL.

    The same URL that worked a moment ago stops working immediately after the
    grant is removed, and the answer is indistinguishable from "there is no such
    source".
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    source_id = upload(api.as_(people.owner), library, BOOK, key).json()["source"]["id"]
    client = api.as_(people.owner)
    assert client.get(f"/sources/{source_id}/original").status_code == 200

    world.revoke(library, people.owner)
    after = client.get(f"/sources/{source_id}/original")
    assert after.status_code == 404

    never_existed = client.get(f"/sources/{uuid4_never()}/original")
    assert never_existed.status_code == 404
    assert after.json() == never_existed.json(), "the two refusals differ"


def uuid4_never():
    import uuid

    return uuid.uuid4()


# ============================================================ durability


def test_the_original_survives_a_restart_of_the_gateway(
    make_api, c10_dsn, store, people, world, key: str
) -> None:
    """Acceptance 4.

    "Restart" here means everything in-process is thrown away: a brand new
    application, a brand new pool, a brand new ``LocalBlobStore`` object over the
    same directory. What survives is on the volume and in PostgreSQL, which is
    the only place it is allowed to survive.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    source_id = upload(make_api(people.owner), library, BOOK, key).json()["source"]["id"]

    from psycopg_pool import ConnectionPool

    from kb.catalog.storage import LocalBlobStore

    fresh_store = LocalBlobStore(store.root)
    fresh_pool = ConnectionPool(
        c10_dsn,
        min_size=1,
        max_size=1,
        open=True,
        kwargs={"autocommit": False, "options": "-c role=kb_app"},
    )
    fresh_pool.wait(timeout=30)
    try:
        restarted = make_api(people.owner, pool=fresh_pool, store_=fresh_store)
        response = restarted.get(f"/sources/{source_id}/original")
        assert response.status_code == 200
        assert response.content == BOOK
    finally:
        fresh_pool.close()


# =============================================== the database says no as well


def test_a_manifest_whose_hash_differs_from_its_source_is_refused(
    run_sql, people, world, key: str
) -> None:
    """The cross-table check the writer performs, exercised from outside it.

    A PostgreSQL CHECK cannot span two tables, so the writer compares the hashes
    with an ``INSERT ... SELECT ... WHERE`` and treats a zero row count as a
    failure. This drives the same statement directly as ``kb_app`` to prove the
    comparison is in the statement and not merely in the Python around it.
    """
    from uuid import uuid4

    library = world.open_library(name="ref", who=people.owner, role="contributor")
    object_key = f"blobs/{library.hex}/{BOOK_HASH[:2]}/{BOOK_HASH[2:4]}/{BOOK_HASH}"
    _rc, out = run_sql(
        "INSERT INTO kb.source (id, library_id, title, media_type, submitted_by, "
        "object_key, content_hash) "
        "VALUES (%s, %s, 't', 'application/pdf', %s, %s, %s);",
        role="kb_app",
        principal=people.owner,
        params=(uuid4(), library, people.owner, object_key, BOOK_HASH),
    )
    assert "row-level security" not in out.lower(), out
    _rc, source_id = run_sql(
        "SELECT id FROM kb.source WHERE object_key = %s;", params=(object_key,)
    )
    source_id = source_id.strip()

    # The statement the writer uses: the manifest is written only when the hash
    # it is about to write is the hash the source already has. Feeding it a
    # different hash writes nothing at all, and the writer treats rowcount == 0
    # as a failure rather than as a success.
    wrong = "f" * 64
    _rc, out = run_sql(
        "INSERT INTO kb.object_manifest "
        "(object_key, backend_key, source_id, content_hash, byte_size, media_type) "
        "SELECT %s, 'local', %s, %s, 10, 'application/pdf' "
        "FROM kb.source s WHERE s.id = %s AND s.object_key = %s AND s.content_hash = %s;",
        role="kb_app",
        principal=people.owner,
        params=(object_key, source_id, wrong, source_id, object_key, wrong),
    )
    assert out.strip() == "", f"the mismatched manifest was accepted: {out}"
    assert (
        world.count(
            "SELECT count(*) FROM kb.object_manifest m JOIN kb.source s ON s.id = m.source_id "
            "WHERE s.library_id = %s",
            (library,),
        )
        == 0
    )


def test_an_insert_the_rls_filtered_is_not_read_as_success(run_sql, people, world) -> None:
    """C06's fact, applied to this card.

    An INSERT under RLS is *rejected* and an UPDATE is *filtered*: a filtered
    write reports zero rows and raises nothing. This card checks every row count
    it depends on; this case shows what it is checking against.
    """
    from uuid import uuid4

    library = world.open_library(name="ref", who=people.owner, role="reader")
    rc, out = run_sql(
        "INSERT INTO kb.object_manifest "
        "(object_key, backend_key, source_id, content_hash, byte_size, media_type) "
        "VALUES (%s, 'local', %s, %s, 1, 'text/plain');",
        role="kb_app",
        principal=people.owner,
        params=(f"blobs/{uuid4().hex}", uuid4(), "a" * 64),
    )
    assert rc != 0
    assert "row-level security" in out.lower(), out
    assert (
        world.count(
            "SELECT count(*) FROM kb.object_manifest m JOIN kb.source s ON s.id = m.source_id "
            "WHERE s.library_id = %s",
            (library,),
        )
        == 0
    )


# ============================================================= url route


def _public_resolver(host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_a_url_submission_stores_what_came_back(make_api, people, world) -> None:
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    body = b"%PDF-1.7 fetched from a url"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"Content-Type": "application/pdf"})

    client = make_api(
        people.owner,
        source_transport=httpx.MockTransport(handler),
        source_resolver=_public_resolver,
    )
    response = client.post(
        f"/libraries/{library}/sources/from-url",
        json={"url": "https://public.test/a-book.pdf", "idempotency_key": "idem-url-0001"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    digest = hashlib.sha256(body).hexdigest()
    assert payload["source"]["content_hash"] == digest
    assert payload["source"]["title"] == "a-book.pdf"
    assert payload["url"] == "https://public.test/a-book.pdf"
    assert payload["retrieved_at"] is not None, "a fetch really did happen"
    assert payload["original"]["byte_size"] == len(body)


def test_a_refused_url_is_a_400_and_leaves_no_row(make_api, people, world) -> None:
    """The route's answer to a loopback URL, over the real fetch path.

    No mock transport is configured, so this is the production code path; it is
    refused before a socket exists, which is why the test needs no network and
    why it cannot be vacuous.
    """
    library = world.open_library(name="ref", who=people.owner, role="contributor")
    client = make_api(people.owner)
    response = client.post(
        f"/libraries/{library}/sources/from-url",
        json={"url": "http://127.0.0.1:9/x", "idempotency_key": "idem-url-0002"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "address_not_allowed"
    assert world.sources_in(library) == []
    assert world.ingests_in(library) == [], "a refused fetch must leave no attempt behind"
