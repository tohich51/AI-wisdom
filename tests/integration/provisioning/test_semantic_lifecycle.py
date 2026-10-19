"""C16 semantic-build lifecycle proof against a real PostgreSQL catalogue."""

from uuid import UUID, uuid4

import psycopg
import pytest

from kb.access.policy import Principal
from kb.retrieval import semantic_registry as registry
from kb.retrieval.embedding_profile import SELECTED_PROFILE

pytestmark = pytest.mark.integration


def _ready_account(world, admin, library_id: UUID) -> None:
    world.insert_account(
        library_id,
        state="ready",
        dimension=SELECTED_PROFILE.dimension,
        embedding_profile=SELECTED_PROFILE.name,
    )
    admin.execute(
        "UPDATE kb.index_account SET profile_hash=%s WHERE library_id=%s",
        (SELECTED_PROFILE.profile_hash, library_id),
    )


def _history(admin, library_id: UUID, generation: int):
    row = admin.execute(
        """SELECT started_by, started_at, canary_uri, content_hash, profile_hash,
                  indexed_revision, manifest_hash, resource_root, indexed_documents,
                  verified_at, failure_reason
           FROM kb.index_generation
           WHERE library_id=%s AND generation=%s""",
        (library_id, generation),
    ).fetchone()
    assert row is not None
    return row


def _checkpoints(admin, library_id: UUID, generation: int):
    return admin.execute(
        """SELECT knowledge_id, resource_uri, statement_hash
           FROM kb.index_document
           WHERE library_id=%s AND generation=%s
           ORDER BY knowledge_id""",
        (library_id, generation),
    ).fetchall()


@pytest.fixture
def semantic_library(world, admin):
    world.account_id = world.organisation()
    for person in (world.owner, world.colleague):
        admin.execute(
            """INSERT INTO kb.membership(
                   organisation_id, principal_id, issuer, subject, status
               ) VALUES(%s,%s,'https://synthetic.invalid',%s,'active')""",
            (world.account_id, person, str(person)),
        )
    library_id, _people = world.library_with(
        ("owner", "manager"),
        ("colleague", "reader"),
    )
    _ready_account(world, admin, library_id)
    world.knowledge(library_id, title="Lifecycle document one")
    return library_id


def test_replacement_manager_resumes_same_generation_then_can_cancel_without_rewriting_history(
    semantic_library, provisioner, world, admin
):
    world.knowledge(semantic_library, title="Lifecycle document two")
    original = world.manager
    snapshot = registry.capture(provisioner, original, semantic_library)
    build = registry.begin(provisioner, original, snapshot)
    first, second = snapshot.documents
    registry.checkpoint(provisioner, original, build, first)

    original_history = _history(admin, semantic_library, build.generation)
    assert original_history[0] == world.owner

    admin.execute(
        """DELETE FROM kb.membership
           WHERE organisation_id=%s AND principal_id=%s""",
        (world.account_id, world.owner),
    )
    world.grant(semantic_library, world.colleague, "manager")
    replacement = world.principal(world.colleague)

    resumed_snapshot = registry.capture(provisioner, replacement, semantic_library)
    resumed = registry.begin(provisioner, replacement, resumed_snapshot)
    assert resumed.generation == build.generation
    assert not resumed.already_current
    assert registry.completed_documents(provisioner, replacement, resumed) == {
        first.id: first.statement_hash
    }
    assert _history(admin, semantic_library, resumed.generation) == original_history

    registry.checkpoint(provisioner, replacement, resumed, second)
    history_before_cancel = _history(admin, semantic_library, resumed.generation)
    checkpoints_before_cancel = _checkpoints(admin, semantic_library, resumed.generation)
    registry.fail(provisioner, replacement, resumed)

    state, finished_at = admin.execute(
        "SELECT state,finished_at FROM kb.index_generation WHERE library_id=%s AND generation=%s",
        (semantic_library, resumed.generation),
    ).fetchone()
    assert state == "retired" and finished_at is not None
    assert _history(admin, semantic_library, resumed.generation) == history_before_cancel
    assert _checkpoints(admin, semantic_library, resumed.generation) == checkpoints_before_cancel
    assert len(checkpoints_before_cancel) == 2
    assert _history(admin, semantic_library, resumed.generation)[0] == world.owner


@pytest.mark.parametrize("mismatch", ["organisation", "account"])
def test_replacement_cancellation_rejects_wrong_organisation_or_app_account(
    semantic_library, provisioner, world, admin, mismatch
):
    build = registry.begin(
        provisioner,
        world.manager,
        registry.capture(provisioner, world.manager, semantic_library),
    )
    world.grant(semantic_library, world.colleague, "manager")

    if mismatch == "organisation":
        other_organisation = uuid4()
        admin.execute(
            "INSERT INTO kb.organisation(id,name) VALUES(%s,%s)",
            (other_organisation, f"wrong-org-{other_organisation.hex[:8]}"),
        )
        admin.execute(
            """INSERT INTO kb.membership(
                   organisation_id, principal_id, issuer, subject, status
               ) VALUES(%s,%s,'https://synthetic.invalid',%s,'active')""",
            (other_organisation, world.colleague, str(world.colleague)),
        )
        admin.execute(
            "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
            (world.account_id, world.colleague),
        )
        actor = world.principal(world.colleague)
    else:
        actor = Principal(
            principal_id=world.colleague,
            account_id=uuid4(),
            generation_watermark=1,
        )

    with pytest.raises(ValueError, match="projection_cancellation_refused"):
        registry.fail(provisioner, actor, build)

    state, started_by = admin.execute(
        "SELECT state,started_by FROM kb.index_generation WHERE library_id=%s AND generation=%s",
        (semantic_library, build.generation),
    ).fetchone()
    assert state == "building" and started_by == world.owner

    registry.fail(provisioner, world.manager, build)


@pytest.mark.parametrize("unavailability", ["external_policy", "no_manager"])
def test_unavailable_context_is_retired_and_releases_the_single_build_slot(
    semantic_library, provisioner, world, admin, unavailability
):
    snapshot = registry.capture(provisioner, world.manager, semantic_library)
    build = registry.begin(provisioner, world.manager, snapshot)
    registry.checkpoint(provisioner, world.manager, build, snapshot.documents[0])
    history = _history(admin, semantic_library, build.generation)
    checkpoints = _checkpoints(admin, semantic_library, build.generation)

    if unavailability == "external_policy":
        admin.execute(
            "INSERT INTO kb.library_policy(library_id,external_processing_allowed) "
            "VALUES(%s,false) ON CONFLICT(library_id) DO UPDATE "
            "SET external_processing_allowed=false",
            (semantic_library,),
        )
    else:
        admin.execute(
            "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
            (world.account_id, world.owner),
        )

    assert registry.resolve_context(provisioner, semantic_library) is None
    assert semantic_library not in registry.pending_libraries(provisioner)
    assert registry.retire_unavailable_builds(provisioner) == 1

    assert (
        admin.execute(
            "SELECT state FROM kb.index_generation WHERE library_id=%s AND generation=%s",
            (semantic_library, build.generation),
        ).fetchone()[0]
        == "retired"
    )
    assert _history(admin, semantic_library, build.generation) == history
    assert _checkpoints(admin, semantic_library, build.generation) == checkpoints

    next_library = world.library()
    _ready_account(world, admin, next_library)
    world.knowledge(next_library, title="Next library after retirement")
    world.generation_policy(next_library, allowed=True, requires_owner_start=False)
    if unavailability == "no_manager":
        world.grant(next_library, world.colleague, "manager")
        starter = world.principal(world.colleague)
    else:
        starter = world.manager

    world.grant(next_library, starter.principal_id, "manager")
    next_snapshot = registry.capture(provisioner, starter, next_library)
    next_build = registry.begin(provisioner, starter, next_snapshot)
    for document in next_snapshot.documents:
        registry.checkpoint(provisioner, starter, next_build, document)
    registry.activate(provisioner, starter, next_build)
    assert admin.execute(
        "SELECT state,started_by FROM kb.index_generation WHERE library_id=%s AND generation=%s",
        (next_library, next_build.generation),
    ).fetchone() == ("current", starter.principal_id)


@pytest.mark.parametrize("busy_lock", ["writer", "library"])
def test_cleanup_returns_zero_for_busy_lock_then_retries_after_release(
    semantic_library, provisioner, world, admin, c15_dsn, busy_lock
):
    build = registry.begin(
        provisioner,
        world.manager,
        registry.capture(provisioner, world.manager, semantic_library),
    )
    admin.execute(
        "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
        (world.account_id, world.owner),
    )
    assert registry.resolve_context(provisioner, semantic_library) is None

    with psycopg.connect(c15_dsn) as blocker:
        if busy_lock == "writer":
            blocker.execute("SELECT pg_advisory_xact_lock(112187,1)")
        else:
            blocker.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,91))",
                (str(semantic_library),),
            )
        assert registry.retire_unavailable_builds(provisioner) == 0
        assert (
            admin.execute(
                "SELECT state FROM kb.index_generation WHERE library_id=%s AND generation=%s",
                (semantic_library, build.generation),
            ).fetchone()[0]
            == "building"
        )

    assert registry.retire_unavailable_builds(provisioner) == 1
    assert (
        admin.execute(
            "SELECT state FROM kb.index_generation WHERE library_id=%s AND generation=%s",
            (semantic_library, build.generation),
        ).fetchone()[0]
        == "retired"
    )


@pytest.mark.parametrize("runtime_role", ["kb_app", "kb_worker"])
def test_runtime_roles_cannot_retire_unavailable_builds(
    semantic_library, provisioner, world, admin, gateway, c15_dsn, runtime_role
):
    build = registry.begin(
        provisioner,
        world.manager,
        registry.capture(provisioner, world.manager, semantic_library),
    )
    admin.execute(
        "INSERT INTO kb.library_policy(library_id,external_processing_allowed) "
        "VALUES(%s,false) ON CONFLICT(library_id) DO UPDATE "
        "SET external_processing_allowed=false",
        (semantic_library,),
    )

    worker = None
    connection = gateway
    if runtime_role == "kb_worker":
        worker = psycopg.connect(
            c15_dsn,
            autocommit=True,
            options="-c role=kb_worker",
        )
        connection = worker
    try:
        with pytest.raises(psycopg.Error) as denied:
            connection.execute(
                "SELECT kb.retire_unavailable_semantic_builds(%s)",
                (SELECTED_PROFILE.profile_hash,),
            )
        assert denied.value.sqlstate == "42501"
    finally:
        if worker is not None:
            worker.close()

    assert (
        admin.execute(
            "SELECT state FROM kb.index_generation WHERE library_id=%s AND generation=%s",
            (semantic_library, build.generation),
        ).fetchone()[0]
        == "building"
    )
    assert registry.retire_unavailable_builds(provisioner) == 1


def test_retirement_rejects_a_non_lower_hex_profile(provisioner):
    with pytest.raises(psycopg.Error) as invalid:
        provisioner.execute(
            "SELECT kb.retire_unavailable_semantic_builds(%s)",
            ("g" * 64,),
        )
    assert invalid.value.sqlstate == "22023"


@pytest.mark.parametrize("account_kind", ["wrong_uuid", "missing"])
def test_original_starter_cancel_requires_matching_transaction_account(
    semantic_library, provisioner, world, admin, account_kind
):
    build = registry.begin(
        provisioner,
        world.manager,
        registry.capture(provisioner, world.manager, semantic_library),
    )
    admin.execute(
        "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
        (world.account_id, world.owner),
    )

    account_value = str(uuid4()) if account_kind == "wrong_uuid" else ""
    with provisioner.transaction():
        provisioner.execute(
            "SELECT set_config('app.principal', %s, true)",
            (str(world.owner),),
        )
        provisioner.execute(
            "SELECT set_config('app.account', %s, true)",
            (account_value,),
        )
        assert (
            provisioner.execute(
                "SELECT kb.cancel_semantic_build(%s, %s)",
                (semantic_library, build.generation),
            ).fetchone()[0]
            is False
        )

    assert (
        admin.execute(
            "SELECT state FROM kb.index_generation WHERE library_id=%s AND generation=%s",
            (semantic_library, build.generation),
        ).fetchone()[0]
        == "building"
    )

    registry.fail(provisioner, world.manager, build)
    assert (
        admin.execute(
            "SELECT state FROM kb.index_generation WHERE library_id=%s AND generation=%s",
            (semantic_library, build.generation),
        ).fetchone()[0]
        == "retired"
    )
