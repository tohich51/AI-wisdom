"""C15 — a half-finished provisioning run resumes, and leaks no key.

Acceptance criterion 3: *частичный provisioning возобновляется без утечки
ключей*. Two halves, tested here.

**Resume.** The run journal in ``kb.provisioning_run`` says which steps
happened. A second run continues from the first unfinished one. What it must
not do is start over: a second account, a second set of identities, a second
key per identity. Key rotation is exactly the thing a retried job must not do
behind an operator's back.

**No leakage.** The keys the admin port hands out are recognisable markers
(``doubles.KEY_MARKER``). After a clean run, a resumed run and a failed one,
this file asserts the marker appears in no row of any table in the schema, in
no log line, in no returned object. The journal additionally has a database
constraint that refuses key-shaped strings, so the guarantee is both a test
and a rule.

The admin port is a test double and says so in its own docstring. What is
verified here is the orchestration and the bookkeeping against a real
PostgreSQL 16.2; nothing about OpenViking, which cannot run here.
"""

from __future__ import annotations

import logging
import uuid

import psycopg
import pytest
from doubles import KEY_MARKER, RecordingIndexAdmin, RecordingSecretSink
from psycopg import sql

from kb.retrieval import provisioning
from kb.retrieval.provisioning import (
    ProvisioningError,
    SecretRef,
    build_account_spec,
    credential_refs,
    load_account,
    request_provisioning,
)

pytestmark = pytest.mark.integration


def _scan_for_marker(conn) -> list[str]:
    """Every text-ish column in the schema that holds the key marker.

    Not "the columns I remember to check": every text column of every table in
    ``kb``, read through the migration role, so a leak into an unexpected
    corner is still found. The table and column names come from
    ``information_schema`` and are composed as identifiers, not interpolated.
    """
    found: list[str] = []
    columns = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'kb' AND data_type IN ('text','character varying','jsonb','json') "
        "ORDER BY table_name, column_name"
    ).fetchall()
    for table, column, _data_type in columns:
        probe = sql.SQL("SELECT 1 FROM {}.{} WHERE {}::text LIKE %s LIMIT 1").format(
            sql.Identifier("kb"),
            sql.Identifier(table),
            sql.Identifier(column),
        )
        needle = "%" + KEY_MARKER + "%"
        if conn.execute(probe, (needle,)).fetchall():
            found.append(f"{table}.{column}")
    return found


def _request(gateway_dsn: str, world, library) -> object:
    """File the request as the gateway would, over its own role."""
    with psycopg.connect(gateway_dsn, autocommit=True) as gateway:
        return request_provisioning(gateway, world.manager, library)


def test_a_clean_run_creates_the_account_identities_acl_and_two_references(
    provisioner, gateway_dsn, admin, world
):
    library, _people = world.library_with(("owner", "manager"))
    request = _request(gateway_dsn, world, library)

    admin_port = RecordingIndexAdmin()
    sink = RecordingSecretSink()
    outcome = provisioning.run_provisioning(
        provisioner, world.manager, request.id, admin_port, sink
    )

    assert [s.step for s in outcome.steps] == list(provisioning.STEPS)
    assert all(s.state == "done" for s in outcome.steps), outcome.steps
    assert outcome.resumed is False

    stored = load_account(provisioner, world.manager, library)
    assert stored is not None
    assert stored.state == "ready"
    assert len(credential_refs(provisioner, world.manager, library)) == 2
    assert sink.secrets() == [KEY_MARKER, KEY_MARKER]
    # exactly one key per identity, never two
    assert sorted(admin_port.keys_issued) == sorted([stored.read_identity, stored.index_identity])


def test_a_run_interrupted_at_the_acl_resumes_and_does_not_reissue_keys(
    provisioner, gateway_dsn, admin, world
):
    """The card's own criterion, end to end.

    The first attempt dies at ``apply_acl``. The second attempt runs with a
    working port. What must be true afterwards: one account, two identities,
    two keys issued *in total* — the resumed run does not mint a second key
    just because it is a second run.
    """
    library, _people = world.library_with(("owner", "manager"))
    request = _request(gateway_dsn, world, library)

    failing = RecordingIndexAdmin(fail_at="apply_acl")
    with pytest.raises(ProvisioningError, match="injected failure"):
        provisioning.run_provisioning(
            provisioner, world.manager, request.id, failing, RecordingSecretSink()
        )

    # The account and the identities are already done; the run stopped before
    # any key existed.
    assert sorted(provisioning.completed_steps(provisioner, world.manager, request.id)) == [
        "account",
        "identities",
    ]
    assert credential_refs(provisioner, world.manager, library) == []

    working = RecordingIndexAdmin()
    sink = RecordingSecretSink()
    outcome = provisioning.run_provisioning(provisioner, world.manager, request.id, working, sink)

    assert outcome.resumed is True
    skipped = [s.step for s in outcome.steps if s.state == "skipped"]
    assert skipped == ["account", "identities"], outcome.steps
    # The resume created no second account and no second set of identities.
    assert working.calls_named("create_account") == []
    assert working.calls_named("ensure_service_identity") == []
    assert len(working.keys_issued) == 2
    assert len(sink.written) == 2
    assert load_account(provisioner, world.manager, library).state == "ready"
    assert _scan_for_marker(admin) == []


def test_a_failure_after_the_keys_does_not_mint_more_on_the_retry(
    provisioner, gateway_dsn, admin, world
):
    """The dangerous window: keys issued, verification never happened.

    This is where a naive "always issue on demand" implementation hands out a
    fresh credential on every retry. A stored reference short-circuits the
    whole step instead.
    """
    library, _people = world.library_with(("owner", "manager"))
    request = _request(gateway_dsn, world, library)

    dying_at_verify = RecordingIndexAdmin(fail_at="describe_account")
    sink_a = RecordingSecretSink()
    with pytest.raises(ProvisioningError):
        provisioning.run_provisioning(
            provisioner, world.manager, request.id, dying_at_verify, sink_a
        )
    assert len(sink_a.written) == 2
    assert len(credential_refs(provisioner, world.manager, library)) == 2

    retry = RecordingIndexAdmin()
    sink_b = RecordingSecretSink()
    outcome = provisioning.run_provisioning(provisioner, world.manager, request.id, retry, sink_b)
    assert outcome.resumed is True
    assert retry.keys_issued == [], retry.keys_issued
    assert sink_b.written == [], sink_b.written
    assert len(credential_refs(provisioner, world.manager, library)) == 2
    assert load_account(provisioner, world.manager, library).state == "ready"


def test_a_crash_between_storing_a_key_and_journalling_it_does_not_rotate_the_key(
    provisioner, gateway_dsn, world
):
    """The narrowest window there is, and the one the code claims to survive.

    ``_store_credential_ref`` commits, then the step is journalled. A process
    that dies between those two leaves a stored reference with no journal entry
    — so the resumed step runs again, and the only thing that stops it from
    minting a second credential is the reference check at the top of the step.

    The crash is simulated by deleting the journal row, which is exactly the
    state that window leaves behind. Without the check this test fails, and it
    is the only test that does: every other resume test is covered by the
    journal alone, which is why this one exists.
    """
    library, _people = world.library_with(("owner", "manager"))
    request = _request(gateway_dsn, world, library)

    first = RecordingIndexAdmin()
    provisioning.run_provisioning(
        provisioner, world.manager, request.id, first, RecordingSecretSink()
    )
    assert len(first.keys_issued) == 2

    # the state a process death between the store and the journal would leave
    world.conn.execute(
        "DELETE FROM kb.provisioning_run WHERE request_id = %s AND step = 'secrets'",
        (request.id,),
    )
    world.conn.execute(
        "UPDATE kb.provisioning_request SET state = 'running', finished_at = NULL WHERE id = %s",
        (request.id,),
    )

    retry = RecordingIndexAdmin()
    sink = RecordingSecretSink()
    outcome = provisioning.run_provisioning(provisioner, world.manager, request.id, retry, sink)
    assert retry.keys_issued == [], "a resumed step rotated a key that already existed"
    assert sink.written == []
    assert outcome.resumed is True
    # 'secrets' re-ran (it was the step whose journal entry was lost) and it
    # issued nothing; 'verified' was already journalled, so it was skipped.
    assert [s.step for s in outcome.steps if s.state == "done"] == ["secrets"]
    assert [s.step for s in outcome.steps if s.state == "skipped"] == [
        "account",
        "identities",
        "acl",
        "verified",
    ]


def test_no_key_reaches_the_database_the_journal_or_the_log(
    provisioner, gateway_dsn, admin, world, caplog
):
    """The marker appears nowhere. Anywhere.

    Scanned across every text column of every table in the schema, plus the
    captured log records, plus the serialised result. The journal's own
    database check is the braces around this belt.
    """
    library, _people = world.library_with(("owner", "manager"))
    request = _request(gateway_dsn, world, library)

    admin_port = RecordingIndexAdmin()
    sink = RecordingSecretSink()
    with caplog.at_level(logging.DEBUG):
        outcome = provisioning.run_provisioning(
            provisioner, world.manager, request.id, admin_port, sink
        )

    assert sink.secrets() == [KEY_MARKER, KEY_MARKER], "the double must have issued keys"
    assert _scan_for_marker(admin) == []
    assert KEY_MARKER not in caplog.text
    assert KEY_MARKER not in outcome.model_dump_json()


def test_the_journal_refuses_to_record_a_key(world, gateway_dsn):
    """A key in the run journal fails the INSERT, by constraint.

    ``provisioning_run.detail`` checks every string in the document against
    this codebase's own reference shapes. A key is not one of them, so an
    accidental log line is a failed write rather than a quiet one.
    """
    library, _people = world.library_with(("owner", "manager"))
    request = _request(gateway_dsn, world, library)

    with pytest.raises(psycopg.errors.CheckViolation, match="detail_carries_no_key_material"):
        world.conn.execute(
            "INSERT INTO kb.provisioning_run (request_id, step, state, detail) "
            "VALUES (%s, 'secrets', 'done', %s::jsonb)",
            (request.id, '{"note": "' + KEY_MARKER + '"}'),
        )
    # The same shape carrying a legitimate reference is fine.
    world.conn.execute(
        "INSERT INTO kb.provisioning_run (request_id, step, state, detail) "
        "VALUES (%s, 'account', 'done', %s::jsonb)",
        (request.id, '{"account_ref": "kb-lib-' + "0" * 32 + '"}'),
    )


def test_a_second_run_of_a_finished_request_does_nothing(provisioner, gateway_dsn, world):
    library, _people = world.library_with(("owner", "manager"))
    request = _request(gateway_dsn, world, library)

    first = RecordingIndexAdmin()
    provisioning.run_provisioning(
        provisioner, world.manager, request.id, first, RecordingSecretSink()
    )
    second = RecordingIndexAdmin()
    outcome = provisioning.run_provisioning(
        provisioner, world.manager, request.id, second, RecordingSecretSink()
    )
    assert second.calls == [], second.calls
    assert outcome.resumed is True
    assert [r.path for r in outcome.secret_refs] == [
        r.path for r in credential_refs(provisioner, world.manager, library)
    ]


def test_one_open_request_per_library(gateway, world):
    """A second request while one is open is a resume, not a second job."""
    library, _people = world.library_with(("owner", "manager"))
    first = request_provisioning(gateway, world.manager, library)
    second = request_provisioning(gateway, world.manager, library)
    assert first.id == second.id


def test_a_reader_cannot_file_a_request_for_a_library_they_cannot_manage(gateway, world):
    """The queue is manager-only, and the error does not confirm the library."""
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    with pytest.raises(provisioning.ProvisioningRefused) as caught:
        request_provisioning(gateway, world.reader, library)
    assert "no such library" in str(caught.value)
    assert str(library) not in str(caught.value)


def test_a_request_cannot_be_filed_in_somebody_elses_name(run_sql, world):
    """``requested_by`` is checked against the transport, not trusted."""
    library, _people = world.library_with(("owner", "manager"))
    rc, out = run_sql(
        "INSERT INTO kb.provisioning_request (library_id, requested_by) VALUES (%s, %s)",
        role="kb_app",
        principal=world.owner,
        params=(library, world.stranger),
    )
    assert rc != 0
    assert "row-level security" in out.lower(), out


def test_the_run_journal_is_a_manager_visibility_not_a_secret_channel(run_sql, gateway_dsn, world):
    """A manager can diagnose a stalled run; a reader cannot see it at all.

    The journal is operational state and a manager's job is to unstick a
    stalled queue, so it is readable there. It carries no secret material by
    construction (``detail`` is checked), so this is a progress channel rather
    than a credential channel.
    """
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    request = _request(gateway_dsn, world, library)
    world.conn.execute(
        "INSERT INTO kb.provisioning_run (request_id, step, state, detail) "
        "VALUES (%s, 'account', 'done', %s::jsonb)",
        (request.id, '{"account_ref": "kb-lib-' + "0" * 32 + '"}'),
    )
    statement = "SELECT count(*) FROM kb.provisioning_run WHERE request_id = %s"
    rc, manager_view = run_sql(
        statement, role="kb_app", principal=world.owner, params=(request.id,)
    )
    assert rc == 0
    assert manager_view.strip() == "1", manager_view
    rc, reader_view = run_sql(
        statement, role="kb_app", principal=world.colleague, params=(request.id,)
    )
    assert rc == 0
    assert reader_view.strip() == "0", reader_view
    rc, stranger_view = run_sql(
        statement, role="kb_app", principal=world.stranger, params=(request.id,)
    )
    assert rc == 0
    assert stranger_view.strip() == "0", stranger_view


def test_the_secret_reference_is_a_path_and_not_a_value():
    """The recorded reference is the sink's path, recomputable from the name."""
    library = uuid.uuid4()
    spec = build_account_spec(library)
    ref = provisioning.secret_ref_for(spec.read_identity)
    assert isinstance(ref, SecretRef)
    assert ref.path == f"kb-secrets/{spec.read_identity}/key"
    assert KEY_MARKER not in ref.path
