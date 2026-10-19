"""Incremental native-copy regressions against real PostgreSQL projections."""

from uuid import uuid4

import pytest
from test_semantic_projection import current
from test_semantic_projection import semantic_library as semantic_library

from kb.access.policy import Principal, transaction_identity
from kb.adapters.vector_writer import IndexCopyPending
from kb.jobs import indexer
from kb.retrieval import semantic_registry as registry

pytestmark = pytest.mark.integration


class SDKWriterDouble:
    def __init__(self):
        self.files, self.copies, self.writes = {}, [], []
        self.lose_next_copy = False

    def prepare(self, root):
        return None

    def copy_document(self, target_root, source_root, document_pair):
        pair = tuple(document_pair)
        self.copies.append((target_root, source_root, pair))
        source_id, statement = pair
        assert source_root in self.files and source_id in self.files[source_root]
        assert self.files[source_root][source_id] == statement
        self.files.setdefault(target_root, {})[source_id] = statement
        if self.lose_next_copy:
            self.lose_next_copy = False
            raise IndexCopyPending()

    def write_batch(self, root, document_pairs):
        pairs = tuple(document_pairs)
        self.writes.append((root, pairs))
        for document_id, statement in pairs:
            self.files.setdefault(root, {})[document_id] = statement

    def verify(self, root, document_pairs):
        stored = self.files.get(root, {})
        assert all(stored.get(pair[0]) == pair[1] for pair in document_pairs)

    def canary(self, root, query, uris):
        return None

    def close(self):
        return None


@pytest.fixture
def sdk_writer(monkeypatch, tmp_path, semantic_library):
    writer = SDKWriterDouble()
    monkeypatch.setattr(indexer, "OpenVikingWriter", lambda _url, _key: writer)
    index_root = tmp_path / "index"
    index_root.mkdir(mode=0o700)
    key = index_root / f"kb-svc-index-{semantic_library.hex}" / "key"
    key.parent.mkdir(mode=0o700)
    key.write_text("synthetic-index-key\n", encoding="utf-8")
    key.chmod(0o600)
    return writer, tmp_path


def run_index(conn, principal, library, keys_root):
    return indexer.reindex(
        conn.info.dsn,
        principal,
        library,
        url="opaque://unused",
        keys_root=keys_root,
        terminal_failure=True,
    )


def begin_library(conn, principal, library):
    return registry.begin(conn, principal, registry.capture(conn, principal, library))


def checkpoint_rows(conn, principal, build):
    with transaction_identity(conn, principal):
        rows = conn.execute(
            "SELECT knowledge_id,resource_uri,statement_hash FROM kb.index_document "
            "WHERE library_id=%s AND generation=%s",
            (build.snapshot.library_id, build.generation),
        ).fetchall()
        return set(rows)


def test_incremental_build_copies_only_unchanged(
    semantic_library, sdk_writer, provisioner, world, admin, tmp_path
):
    writer, keys_root = sdk_writer
    world.knowledge(semantic_library, title="Retained across generations")
    source_id = uuid4()
    admin.execute(
        "INSERT INTO kb.source(id,library_id,title,media_type,submitted_by,"
        "object_key,content_hash) "
        "VALUES(%s,%s,'Withdrawable synthetic source','text/plain',%s,%s,%s)",
        (source_id, semantic_library, world.owner, source_id.hex, "b" * 64),
    )
    withdrawn = world.knowledge(
        semantic_library, title="Withdraw after first build", publication="draft"
    )
    admin.execute(
        "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id) VALUES(%s,%s)",
        (withdrawn, source_id),
    )
    admin.execute("UPDATE kb.knowledge SET publication='published' WHERE id=%s", (withdrawn,))
    run_index(provisioner, world.manager, semantic_library, keys_root)

    old = current(provisioner, world, semantic_library)
    source_files = dict(writer.files[old.resource_root])
    assert source_files == {d.id: d.statement for d in old.snapshot.documents}
    unchanged = {d.id for d in old.snapshot.documents} - {withdrawn}
    writer.copies, writer.writes = [], []

    added = world.knowledge(semantic_library, title="New after first build")
    admin.execute("UPDATE kb.source SET publication='withdrawn' WHERE id=%s", (source_id,))
    run_index(provisioner, world.manager, semantic_library, keys_root)
    build = current(provisioner, world, semantic_library)

    assert old.generation < build.generation
    assert writer.files[build.resource_root] == {
        d.id: d.statement for d in build.snapshot.documents
    }
    assert writer.files[old.resource_root] == source_files
    expected_copies = {
        (build.resource_root, old.resource_root, (d.id, d.statement))
        for d in build.snapshot.documents
        if d.id in unchanged
    }
    assert len(writer.copies) == len(expected_copies)
    assert {(target, source, pair) for target, source, pair in writer.copies} == expected_copies
    assert len(writer.writes) == 1
    assert [(root, {pair[0] for pair in pairs}) for root, pairs in writer.writes] == [
        (build.resource_root, {added})
    ]
    assert checkpoint_rows(provisioner, world.manager, build) == {
        (d.id, build.document_uri(d), d.statement_hash) for d in build.snapshot.documents
    }


def test_lost_copy_response_preserves_generation_for_same_retry(
    semantic_library, sdk_writer, provisioner, world, tmp_path
):
    writer, keys_root = sdk_writer
    run_index(provisioner, world.manager, semantic_library, keys_root)
    old = current(provisioner, world, semantic_library)
    old_checkpoints = checkpoint_rows(provisioner, world.manager, old)
    unchanged = old.snapshot.documents[0]
    writer.copies, writer.writes = [], []
    changed = world.knowledge(semantic_library, title="Requires a new embedding")
    writer.lose_next_copy = True

    with pytest.raises(IndexCopyPending):
        run_index(provisioner, world.manager, semantic_library, keys_root)

    build = begin_library(provisioner, world.manager, semantic_library)
    operation = (
        build.resource_root,
        old.resource_root,
        (unchanged.id, unchanged.statement),
    )
    assert writer.copies == [operation]
    assert writer.files[build.resource_root] == {unchanged.id: unchanged.statement}
    assert writer.writes == []
    with transaction_identity(provisioner, world.manager):
        states = dict(
            provisioner.execute(
                "SELECT generation,state FROM kb.index_generation WHERE library_id=%s",
                (semantic_library,),
            ).fetchall()
        )
    assert (states[old.generation], states[build.generation]) == ("current", "building")
    assert checkpoint_rows(provisioner, world.manager, build) == set()
    assert checkpoint_rows(provisioner, world.manager, old) == old_checkpoints

    writer.copies, writer.writes = [], []
    run_index(provisioner, world.manager, semantic_library, keys_root)
    same = begin_library(provisioner, world.manager, semantic_library)
    assert same.generation == build.generation and same.already_current
    assert writer.copies == [operation]
    assert len(writer.writes) == 1
    assert [(root, {pair[0] for pair in pairs}) for root, pairs in writer.writes] == [
        (same.resource_root, {changed})
    ]
    assert set(writer.files[same.resource_root]) == {unchanged.id, changed}
    assert checkpoint_rows(provisioner, world.manager, old) == old_checkpoints


def test_previous_documents_does_not_cross_account(semantic_library, provisioner, world, admin):
    old = current(provisioner, world, semantic_library)
    world.knowledge(semantic_library, title="Create a distinct building generation")
    pending = begin_library(provisioner, world.manager, semantic_library)
    source_root, hashes = registry.previous_documents(provisioner, world.manager, pending)
    assert source_root == old.resource_root
    assert hashes == {d.id: d.statement_hash for d in old.snapshot.documents}

    other_account = uuid4()
    admin.execute(
        "INSERT INTO kb.organisation(id,name) VALUES(%s,'Other copy context')", (other_account,)
    )
    # The same granted principal in another account must not inherit this library's mapping.
    outsider = Principal(world.owner, other_account)
    try:
        assert registry.previous_documents(provisioner, outsider, pending) == (None, {})
    finally:
        registry.fail(provisioner, world.manager, pending)
