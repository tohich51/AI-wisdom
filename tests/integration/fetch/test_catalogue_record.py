"""C12B — what the catalogue records about a page, and who is allowed to.

Acceptance item 3 is here: "Повторное получение создаёт понятную новую версию" —
fetching the same URL again produces an understandable new version, and the
first one is never quietly rewritten.

Everything below runs against a **real PostgreSQL 16.2** connected as
``kb_app``, the runtime role, with RLS enabled and forced. A superuser
connection would BYPASS RLS and make every access assertion here pass
vacuously; the only owner connection is the one that arranges the fixtures.

Two of these tests exist because of a real gap in the shipped schema rather than
to prove something convenient. ``kb.fragment`` has a SELECT policy and no write
policy, so ``kb_app`` cannot insert a fragment at all. The fixture
``pending_fragment_write_policy`` applies the proposed policy so the rest of the
suite can run, and the two tests at the end assert that the policy is genuinely
absent from the repository and that without it the insert fails loudly rather
than reporting a success it did not achieve.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest
from harness import PUBLIC, parsed_from, resolver_for, serving, snapshot_over
from pydantic import ValidationError

from kb.access.policy import AccessDenied
from kb.catalog.fetch_snapshots import (
    HTML_MEDIA_TYPE,
    HtmlIngestSpec,
    ingest_html_snapshot,
    read_snapshot,
)
from kb.catalog.storage import StorageError
from kb.contracts.enums import LocatorKind, ProcessingStatus

MIGRATIONS = pathlib.Path(__file__).resolve().parents[3] / "migrations"


def a_snapshot(over_body: bytes, *, url: str = "https://example.test/page", offset: int = 0):
    """A snapshot over a mock transport, stamped at FROZEN_NOW plus ``offset`` days."""
    import datetime as dt

    from harness import FROZEN_NOW

    return snapshot_over(
        serving(over_body),
        url=url,
        resolver=resolver_for({"example.test": [PUBLIC]}),
        now=lambda: FROZEN_NOW + dt.timedelta(days=offset),
    )


PAGE_ONE = (
    b"<html><head><title>Handbook</title></head><body>"
    b"<h1>Handbook</h1><p>First edition.</p></body></html>"
)
PAGE_TWO = (
    b"<html><head><title>Handbook</title></head><body>"
    b"<h1>Handbook</h1><p>Second edition, revised.</p></body></html>"
)


@pytest.fixture
def scenario(world, people, runtime_conn, store):
    """A library with one contributor, ready for one snapshot."""
    library = world.open_library(name="c12b-catalogue", who=people.contributor, role="contributor")
    return {
        "library": library,
        "principal": people.principal(people.contributor),
        "key": f"idem-{uuid.uuid4().hex}",
    }


# ================================================== the first retrieval


def test_a_fetched_page_becomes_a_source_with_its_own_address(
    world, runtime_conn, store, scenario
) -> None:
    """Source, original, version 1, fragments and the attempt, in one transaction."""
    snapshot = a_snapshot(PAGE_ONE)
    parsed = parsed_from(snapshot)
    spec = HtmlIngestSpec(
        library_id=scenario["library"],
        title="Handbook",
        idempotency_key=scenario["key"],
    )
    result = ingest_html_snapshot(
        runtime_conn, scenario["principal"], store, snapshot, parsed, spec
    )

    assert result.status == "stored"
    assert result.version_no == 1
    assert result.is_readable is True
    assert result.extractor_version == parsed.extractor_version

    sources = world.sources_in(scenario["library"])
    assert len(sources) == 1
    source_id, media_type, object_key, content_hash, processing, submitted_by = sources[0]
    assert source_id == result.source_id
    assert media_type == HTML_MEDIA_TYPE
    assert object_key.endswith(content_hash)
    assert processing == str(ProcessingStatus.QUEUED)
    assert submitted_by == scenario["principal"].principal_id

    manifests = world.manifests_in(scenario["library"])
    assert len(manifests) == 1
    assert manifests[0][0] == object_key
    assert manifests[0][1] == snapshot.content_hash
    assert manifests[0][3] == HTML_MEDIA_TYPE

    versions = world.versions_of(source_id)
    assert versions == [(1, snapshot.content_hash)]

    fragments = world.fragments_of(source_id)
    assert len(fragments) == result.fragment_count == 2
    assert [row[0] for row in fragments] == [0, 1]
    assert all(row[1] == str(LocatorKind.HTML_SNAPSHOT) for row in fragments)
    assert all(row[4] == snapshot.final_url for row in fragments)
    assert all(row[5] == snapshot.retrieved_at for row in fragments)
    assert [row[6] for row in fragments] == ["Handbook", "First edition."]
    assert [row[2] for row in fragments] == [1, 2], "the locator ordinal is 1-based"
    assert all(row[3] == "Handbook" for row in fragments), "the heading is the chapter"


def test_the_record_names_the_url_and_the_instant_it_was_retrieved(
    world, runtime_conn, store, scenario
) -> None:
    """A snapshot without its URL and its retrieval time is not a snapshot.

    Both come from the fetch, not from the transaction. The row's ``created_at``
    is the database's clock and is a different fact; the test compares them so a
    future refactor that quietly swaps one for the other fails.
    """
    snapshot = a_snapshot(PAGE_ONE, url="https://example.test/handbook/one")
    result = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )

    row = world.conn.execute(
        "SELECT i.source_url, i.retrieved_at, i.created_at, i.state, i.content_hash "
        "FROM kb.ingest i WHERE i.library_id = %s",
        (scenario["library"],),
    ).fetchone()
    assert row[0] == "https://example.test/handbook/one"
    assert row[1] == snapshot.retrieved_at
    assert row[2] != row[1], "retrieved_at must not be the row's creation time"
    assert row[3] == "committed"
    assert row[4] == snapshot.content_hash

    record = read_snapshot(runtime_conn, scenario["principal"], result.source_id)
    assert record is not None
    assert record.url == "https://example.test/handbook/one"
    assert record.retrieved_at == snapshot.retrieved_at
    assert record.current_version == 1
    assert record.fragment_count == 2


def test_two_retrievals_of_the_same_page_are_one_source(
    world, runtime_conn, store, scenario
) -> None:
    """The same bytes in one library are one source, however many times they arrive."""
    snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    second = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=f"idem-{uuid.uuid4().hex}"),
    )
    assert first.source_id == second.source_id
    assert second.status == "deduplicated"
    assert len(world.sources_in(scenario["library"])) == 1
    assert len(world.ingests_in(scenario["library"])) == 2
    assert second.limitations, "a deduplicated answer says so rather than pretending"


def test_a_page_in_another_library_is_never_disclosed(
    world, people, runtime_conn, store, scenario
) -> None:
    """A20: the answer to a second submission says nothing about the first.

    The same page in a second library is a second source. Neither submitter is
    told that the other one has it, because "somebody else already uploaded this"
    is a disclosure about another user's holdings.
    """
    snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    other_library = world.open_library(name="c12b-other", who=people.contributor)
    second = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=other_library, idempotency_key=f"idem-{uuid.uuid4().hex}"),
    )
    assert second.source_id != first.source_id
    assert "deduplicated" not in second.status
    assert second.status == "stored"
    joined = " ".join(second.limitations) + second.status
    assert scenario["library"].hex not in joined


# ================================================== fetching it a second time


def test_a_changed_page_makes_a_new_version_and_keeps_the_first(
    world, runtime_conn, store, scenario
) -> None:
    """Acceptance item 3.

    The second retrieval produces version 2 with the new hash; version 1 keeps
    the old one and is never rewritten. Both retrievals are in the record with
    their own URL and their own instant, which is what makes the second one
    understandable rather than a silent replacement.
    """
    first_snapshot = a_snapshot(PAGE_ONE, offset=0)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        first_snapshot,
        parsed_from(first_snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )

    second_snapshot = a_snapshot(PAGE_TWO, offset=10)
    second = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        second_snapshot,
        parsed_from(second_snapshot),
        HtmlIngestSpec(
            library_id=scenario["library"],
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            source_id=first.source_id,
        ),
    )

    assert second.status == "versioned"
    assert second.version_no == 2
    assert second.source_id == first.source_id
    assert second.content_hash == second_snapshot.content_hash != first.content_hash

    versions = world.versions_of(first.source_id)
    assert versions == [
        (1, first_snapshot.content_hash),
        (2, second_snapshot.content_hash),
    ], "version 1 must keep its own hash"

    ingests = world.ingests_in(scenario["library"])
    assert len(ingests) == 2
    assert {row[2] for row in ingests} == {first_snapshot.final_url, second_snapshot.final_url}
    assert {row[3] for row in ingests} == {
        first_snapshot.retrieved_at,
        second_snapshot.retrieved_at,
    }

    record = read_snapshot(runtime_conn, scenario["principal"], first.source_id)
    assert record is not None
    assert record.current_version == 2
    assert len(record.versions) == 2


def test_a_version_says_plainly_that_its_text_is_not_stored(
    world, runtime_conn, store, scenario
) -> None:
    """The result does not claim the new page's text is in the catalogue.

    ``kb.fragment`` has no version column and ``kb.source_version`` has no
    object key, so the new version's bytes are deliberately not written and its
    fragments are not stored. Version 1's fragments are untouched. The caller
    gets ``is_readable`` False and two named limitations, not a success message.
    """
    first_snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        first_snapshot,
        parsed_from(first_snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    before = world.fragments_of(first.source_id)

    second_snapshot = a_snapshot(PAGE_TWO, offset=10)
    second = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        second_snapshot,
        parsed_from(second_snapshot),
        HtmlIngestSpec(
            library_id=scenario["library"],
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            source_id=first.source_id,
        ),
    )
    assert second.is_readable is False
    assert second.fragment_count == 0
    assert second.object_key is None
    assert any("kb.fragment has no version column" in limit for limit in second.limitations)
    assert any("kb.source_version has no object key" in limit for limit in second.limitations)
    assert world.fragments_of(first.source_id) == before, "version 1's text was rewritten"


def test_a_version_writes_no_unreferenced_bytes_to_the_volume(
    world, runtime_conn, store, tmp_path, scenario
) -> None:
    """A version has nowhere to put its bytes, so it puts none there.

    An object with no manifest is unreferenced residue — C10 documented it as
    the safe side of the invariant and this card does not add to it.
    """
    first_snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        first_snapshot,
        parsed_from(first_snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    before = sorted(p.name for p in (tmp_path / "objects" / "blobs").rglob("*") if p.is_file())

    second_snapshot = a_snapshot(PAGE_TWO, offset=10)
    ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        second_snapshot,
        parsed_from(second_snapshot),
        HtmlIngestSpec(
            library_id=scenario["library"],
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            source_id=first.source_id,
        ),
    )
    after = sorted(p.name for p in (tmp_path / "objects" / "blobs").rglob("*") if p.is_file())
    assert after == before


def test_refetching_an_unchanged_page_is_not_a_new_version(
    world, runtime_conn, store, scenario
) -> None:
    """A version counts changes, not submissions.

    Saying "version 2" for a page that did not change would make the number a
    count of fetches, and a reader comparing two versions would be told there
    is a difference where there is none.
    """
    first_snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        first_snapshot,
        parsed_from(first_snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    again = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        first_snapshot,
        parsed_from(first_snapshot),
        HtmlIngestSpec(
            library_id=scenario["library"],
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            source_id=first.source_id,
        ),
    )
    assert again.status == "unchanged"
    assert again.version_no == 1
    assert world.versions_of(first.source_id) == [(1, first_snapshot.content_hash)]


def test_a_second_retrieval_keeps_the_first_original_readable(
    world, runtime_conn, store, scenario
) -> None:
    """Version 1's bytes are still there and still re-hash to what they claim."""
    from kb.catalog.upload import read_original

    first_snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        first_snapshot,
        parsed_from(first_snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    second_snapshot = a_snapshot(PAGE_TWO, offset=10)
    ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        second_snapshot,
        parsed_from(second_snapshot),
        HtmlIngestSpec(
            library_id=scenario["library"],
            idempotency_key=f"idem-{uuid.uuid4().hex}",
            source_id=first.source_id,
        ),
    )
    read = read_original(runtime_conn, scenario["principal"], store, first.source_id)
    assert read.content == first_snapshot.content
    assert read.original.content_hash == first_snapshot.content_hash


def test_a_refetch_by_someone_without_a_grant_changes_nothing(
    world, people, runtime_conn, store, scenario
) -> None:
    """A reader may not turn a page into a new version.

    The check runs before a transaction, so the refusal leaves no attempt row
    and no version — an error is not a row.
    """
    first_snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        first_snapshot,
        parsed_from(first_snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    before_versions = world.versions_of(first.source_id)
    before_ingests = world.ingests_in(scenario["library"])

    second_snapshot = a_snapshot(PAGE_TWO, offset=10)
    with pytest.raises(AccessDenied):
        ingest_html_snapshot(
            runtime_conn,
            people.principal(people.reader),
            store,
            second_snapshot,
            parsed_from(second_snapshot),
            HtmlIngestSpec(
                library_id=scenario["library"],
                idempotency_key=f"idem-{uuid.uuid4().hex}",
                source_id=first.source_id,
            ),
        )
    assert world.versions_of(first.source_id) == before_versions
    assert world.ingests_in(scenario["library"]) == before_ingests


def test_a_refetch_of_a_source_the_caller_cannot_see_is_a_refusal(
    world, people, runtime_conn, store, scenario
) -> None:
    """ "No such source" is the same answer whether it is invisible or absent.

    The caller's own identity scopes the read, so a source in a library they
    have no grant on returns nothing at all and the two cases cannot be told
    apart from outside.
    """
    stranger = people.principal(people.stranger)
    absent = read_snapshot(runtime_conn, stranger, uuid.uuid4())
    with pytest.raises(AccessDenied):
        ingest_html_snapshot(
            runtime_conn,
            stranger,
            store,
            a_snapshot(PAGE_ONE),
            parsed_from(a_snapshot(PAGE_ONE)),
            HtmlIngestSpec(
                library_id=scenario["library"],
                idempotency_key=f"idem-{uuid.uuid4().hex}",
                source_id=uuid.uuid4(),
            ),
        )
    assert absent is None


# ============================================================ who may do this


def test_a_reader_may_not_store_a_page(world, people, runtime_conn, store, scenario) -> None:
    """Contributor or better. A reader makes the gateway write nothing at all."""
    snapshot = a_snapshot(PAGE_ONE)
    with pytest.raises(AccessDenied):
        ingest_html_snapshot(
            runtime_conn,
            people.principal(people.reader),
            store,
            snapshot,
            parsed_from(snapshot),
            HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
        )
    assert world.sources_in(scenario["library"]) == []
    assert world.ingests_in(scenario["library"]) == []


def test_the_stored_page_is_invisible_to_a_stranger(
    world, people, runtime_conn, store, scenario
) -> None:
    """The row exists, and the stranger sees nothing.

    Read through the runtime role with RLS forced, so this is the database
    refusing rather than a query that forgot to ask.
    """
    snapshot = a_snapshot(PAGE_ONE)
    result = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    assert read_snapshot(runtime_conn, scenario["principal"], result.source_id) is not None
    assert read_snapshot(runtime_conn, people.principal(people.stranger), result.source_id) is None


def test_revoking_the_grant_closes_the_snapshot(
    world, people, runtime_conn, store, scenario
) -> None:
    """Access is re-checked on every read; there is no handle that outlives it."""
    snapshot = a_snapshot(PAGE_ONE)
    result = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    before = read_snapshot(runtime_conn, scenario["principal"], result.source_id)
    world.revoke(scenario["library"], people.contributor)
    after = read_snapshot(runtime_conn, scenario["principal"], result.source_id)
    assert before is not None
    assert after is None


# ================================================================= identity


def test_the_spec_carries_no_identity_field() -> None:
    """A body with ``submitted_by`` is refused before a handler runs.

    Identity comes from the transport; ``extra="forbid"`` is what makes that a
    property of the model rather than a habit of the callers.
    """
    with pytest.raises(ValidationError):
        HtmlIngestSpec(
            library_id=uuid.uuid4(),
            idempotency_key="idem-00000000",
            submitted_by=uuid.uuid4(),
        )
    with pytest.raises(ValidationError):
        HtmlIngestSpec(library_id=uuid.uuid4(), idempotency_key="idem-00000000", role="manager")
    with pytest.raises(ValidationError):
        HtmlIngestSpec(library_id=uuid.uuid4(), idempotency_key="idem-00000000", user_id="alice")


def test_the_submitter_is_the_principal_and_nothing_else(
    world, runtime_conn, store, scenario, people
) -> None:
    """The row's ``submitted_by`` is the verified principal, whatever the page says."""
    page = (
        b"<html><body><p>submitted_by = "
        + str(people.stranger).encode()
        + b", role = manager</p></body></html>"
    )
    snapshot = a_snapshot(page)
    result = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    row = world.sources_in(scenario["library"])[0]
    assert row[5] == people.contributor
    assert row[5] == scenario["principal"].principal_id
    assert world.grants(scenario["library"]) == [(people.contributor, "contributor")]
    assert result.status == "stored"


def test_a_fragment_set_from_another_page_is_refused(runtime_conn, store, scenario) -> None:
    """The extraction must be of *this* snapshot, or it is not stored at all.

    A caller that can pass a ``ParsedDocument`` is a caller that could attach
    one page's text to another page's provenance. The three fields that make a
    snapshot a snapshot are compared.
    """
    snapshot = a_snapshot(PAGE_ONE)
    other = a_snapshot(PAGE_TWO, offset=10)
    with pytest.raises(StorageError) as raised:
        ingest_html_snapshot(
            runtime_conn,
            scenario["principal"],
            store,
            snapshot,
            parsed_from(other),
            HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
        )
    assert "different bytes" in str(raised.value)


# =========================================================== idempotency


def test_a_repeated_submission_replays_and_writes_nothing(
    world, runtime_conn, store, scenario
) -> None:
    """A retried submission resolves to the source the first one produced."""
    snapshot = a_snapshot(PAGE_ONE)
    spec = HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"])
    first = ingest_html_snapshot(
        runtime_conn, scenario["principal"], store, snapshot, parsed_from(snapshot), spec
    )
    second = ingest_html_snapshot(
        runtime_conn, scenario["principal"], store, snapshot, parsed_from(snapshot), spec
    )
    assert first.status == "stored"
    assert second.status == "replayed"
    assert second.source_id == first.source_id
    assert len(world.sources_in(scenario["library"])) == 1
    assert len(world.ingests_in(scenario["library"])) == 1


def test_an_idempotency_key_belonging_to_another_submitter_is_refused(
    world, people, runtime_conn, store, scenario
) -> None:
    """RLS is the enforcement, so a guessed key reveals nothing.

    A second contributor in the same library guesses the key the first one used.
    The unique index reports the conflict even though the row stays invisible to
    the guesser, so the answer is the one a caller gets for a key that was never
    used — and no source of the first submitter is named.
    """
    world.grant(scenario["library"], people.owner, "contributor")
    snapshot = a_snapshot(PAGE_ONE)
    first = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    second_page = a_snapshot(PAGE_TWO, offset=1)
    with pytest.raises(AccessDenied):
        ingest_html_snapshot(
            runtime_conn,
            people.principal(people.owner),
            store,
            second_page,
            parsed_from(second_page),
            HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
        )
    assert len(world.sources_in(scenario["library"])) == 1
    assert world.sources_in(scenario["library"])[0][0] == first.source_id
    assert len(world.ingests_in(scenario["library"])) == 1


# ============================================= the schema gap this card found


def test_the_fragment_write_policy_this_card_needs_is_not_in_the_repository() -> None:
    """The gap is real and it is still there.

    Found by running the insert, not by reading the migration: ``kb.fragment``
    has RLS enabled and forced with a SELECT policy and no write policy, so
    ``kb_app`` cannot insert a fragment at all. The fix belongs to the single
    owner of ``migrations/``; this card may not add one. The suite applies the
    proposed policy in a fixture, and this test says so out loud.
    """
    scripts = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("0*.sql"))
    )
    assert "CREATE POLICY fragment_write" not in scripts
    assert "ALTER TABLE kb.fragment" in scripts
    assert "FORCE  ROW LEVEL SECURITY" in scripts or "FORCE ROW LEVEL SECURITY" in scripts


def test_without_that_policy_the_insert_is_refused_loudly(
    world, runtime_conn, store, scenario, admin
) -> None:
    """No silent half-success: without the policy the write fails, visibly.

    The policy is dropped on the owner connection (which is autocommit, so the
    ``runtime_conn`` session really does see it go) and restored in a ``finally``,
    so the suite is left exactly as it was found even if the assertion fails.
    What is asserted is the shape of the failure — a server error naming RLS,
    not a zero row count reported to the caller as a stored page.
    """
    import psycopg
    from harness import PENDING_FRAGMENT_WRITE_POLICY

    snapshot = a_snapshot(PAGE_ONE)
    admin.execute("DROP POLICY IF EXISTS fragment_write ON kb.fragment")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as raised:
            ingest_html_snapshot(
                runtime_conn,
                scenario["principal"],
                store,
                snapshot,
                parsed_from(snapshot),
                HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
            )
        assert "row-level security" in str(raised.value)
    finally:
        admin.execute(PENDING_FRAGMENT_WRITE_POLICY)
    # What is left behind is the residue C10 documents: an attempt row that is
    # not committed and names no source. It describes no readable object, and a
    # retry with the same key resumes it rather than creating a second one.
    assert world.sources_in(scenario["library"]) == []
    ingests = world.ingests_in(scenario["library"])
    assert len(ingests) == 1
    assert ingests[0][0] != "committed"
    assert ingests[0][1] is None, "a failed attempt records no content hash"


class _SilentCursor:
    """A cursor that accepts a write and reports nothing back.

    PostgreSQL raises on a WITH CHECK policy violation, so no policy in the
    shipped schema produces the condition this stands in for: a write that is
    filtered without an exception. A ``USING``-only policy would, and a future
    migration could add one. The check is therefore exercised directly rather
    than left as an untested branch.
    """

    rowcount = 0

    def executemany(self, *_args, **_kwargs) -> None:
        return None

    def execute(self, *_args, **_kwargs) -> None:
        return None

    def fetchone(self):
        return (0,)


def test_a_silently_filtered_fragment_write_is_refused_not_reported_as_stored() -> None:
    """rowcount 0 is a failure, and the failure is loud.

    Without this, a page whose text never reached the catalogue would be
    reported as ``stored`` and the catalogue would be quietly missing it.
    """
    from kb.catalog.fetch_snapshots import _fragments, _write_fragments

    snapshot = a_snapshot(PAGE_ONE)
    source_id = uuid.uuid4()
    fragments = _fragments(source_id, parsed_from(snapshot))
    assert len(fragments) == 2, "the stand-in needs real fragments to reject"

    with pytest.raises(AccessDenied) as raised:
        _write_fragments(_SilentCursor(), source_id, fragments)  # type: ignore[arg-type]
    assert "refused by the database" in str(raised.value)


def test_the_original_is_unreachable_once_its_manifest_is_gone(
    world, people, runtime_conn, store, scenario, admin
) -> None:
    """ "No manifest, no download" still holds for a page.

    The blob and the source row can both be present and the object still cannot
    be read, because ``kb.object_manifest`` is the only path from a key to a
    readable source. Proven by removing the manifest and reading.
    """
    from kb.catalog.upload import read_original

    snapshot = a_snapshot(PAGE_ONE)
    result = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    admin.execute("DELETE FROM kb.object_manifest WHERE source_id = %s", (result.source_id,))
    with pytest.raises(AccessDenied):
        read_original(runtime_conn, scenario["principal"], store, result.source_id)


def test_a_snapshot_survives_a_restart_of_the_gateway(
    world, runtime_conn, store, tmp_path, scenario, people
) -> None:
    """A new store object and a new connection over the same disk and database.

    A page is an immutable original: it is still readable after the process that
    fetched it is gone, and the hash still matches what the catalogue claims. A
    principal with no grant gets the same refusal a missing source gives, and it
    is refused before the store is touched at all.
    """
    import hashlib

    from kb.catalog.storage import LocalBlobStore
    from kb.catalog.upload import read_original

    snapshot = a_snapshot(PAGE_ONE)
    result = ingest_html_snapshot(
        runtime_conn,
        scenario["principal"],
        store,
        snapshot,
        parsed_from(snapshot),
        HtmlIngestSpec(library_id=scenario["library"], idempotency_key=scenario["key"]),
    )
    restarted = LocalBlobStore(tmp_path / "objects")
    read = read_original(runtime_conn, scenario["principal"], restarted, result.source_id)
    assert hashlib.sha256(read.content).hexdigest() == snapshot.content_hash
    assert read.content == snapshot.content

    with pytest.raises(AccessDenied):
        read_original(runtime_conn, people.principal(people.stranger), restarted, result.source_id)
