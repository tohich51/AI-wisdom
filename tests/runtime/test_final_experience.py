"""C23: explicit assessments and issuance audit, using synthetic real-PG fixtures."""

from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_runtime import rule, source
from test_runtime import runtime_db as runtime_db
from test_runtime import world as world

from kb.access.policy import AccessDenied, Principal, transaction_identity
from kb.catalog import writes
from kb.catalog.retrieval_events import record_retrieval

pytestmark = pytest.mark.integration


def applied_use(world):
    published = rule(world)
    with psycopg.connect(world["dsn"]) as conn:
        use = writes.start_use(conn, world["principal"], published["id"], 1)
        writes.mark_applied(conn, world["principal"], use["id"])
    return use["id"]


def knowledge(world, *, published=True):
    with psycopg.connect(world["dsn"]) as conn, transaction_identity(conn, world["principal"]):
        ident = conn.execute(
            "INSERT INTO kb.knowledge(library_id,statement,kind) "
            "VALUES(%s,'Public synthetic C23 definition','definition') RETURNING id",
            (world["lib"],),
        ).fetchone()[0]
        if published:
            writes.publish(conn, world["principal"], ident, knowledge=True)
        return ident


def test_achievement_only_is_separate_from_usefulness_and_measurement(world):
    use_id = applied_use(world)
    with psycopg.connect(world["dsn"]) as conn:
        first = writes.record_outcome(
            conn, world["principal"], use_id, achievement="met", assessor_kind="human"
        )
        assert first["achievement"] == "met" and first["assessor_kind"] == "human"
        assert first["rating"] is None and first["observed_result"] is None
        assert first["assessment_evidence"] is None
        measured = writes.record_outcome(
            conn,
            world["principal"],
            use_id,
            is_correction=True,
            achievement="partly",
            assessor_kind="measurement",
            assessment_evidence="Synthetic metric: 2 of 3 checks met",
        )
        assert measured["rating"] is None and measured["achievement"] == "partly"
        assert measured["assessor_kind"] == "measurement"
        assert measured["assessment_evidence"] == "Synthetic metric: 2 of 3 checks met"


def test_explicit_unknown_reports_uncertainty_but_omitted_empty_call_does_not(world):
    use_id = applied_use(world)
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(ValueError):
            writes.record_outcome(conn, world["principal"], use_id)
        result = writes.record_outcome(conn, world["principal"], use_id, achievement="unknown")
        assert result["achievement"] == result["assessor_kind"] == "unknown"
        assert (
            result["rating"] is result["observed_result"] is result["assessment_evidence"] is None
        )


@pytest.mark.parametrize(
    "values",
    [
        {"achievement": "achieved"},
        {"assessor_kind": "automatic"},
        {"assessment_evidence": "x" * 4001},
        {"assessment_evidence": 7},
        {"assessor_kind": "measurement"},
        {"assessor_kind": "measurement", "assessment_evidence": " \t\n"},
    ],
)
def test_invalid_assessment_never_persists_an_event(world, values):
    use_id = applied_use(world)
    with psycopg.connect(world["dsn"]) as conn:
        with pytest.raises(ValueError):
            writes.record_outcome(
                conn, world["principal"], use_id, "Synthetic observation", **values
            )
    with psycopg.connect(world["admin"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM kb.outcome WHERE use_id=%s", (use_id,)
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT state FROM kb.use_record WHERE id=%s", (use_id,)
        ).fetchone() == ("applied",)


@pytest.mark.parametrize(
    "changed",
    [
        {"achievement": "partly"},
        {"assessor_kind": "human"},
        {"assessment_evidence": "Other evidence"},
    ],
)
def test_assessment_fields_are_part_of_idempotent_event_identity(world, changed):
    use_id, key = applied_use(world), str(uuid4())
    original = {
        "achievement": "met",
        "assessor_kind": "agent",
        "assessment_evidence": "Synthetic evidence",
    }
    with psycopg.connect(world["dsn"]) as conn:
        first = writes.record_outcome(
            conn, world["principal"], use_id, **original, idempotency_key=key
        )
        assert (
            writes.record_outcome(conn, world["principal"], use_id, **original, idempotency_key=key)
            == first
        )
        with pytest.raises(ValueError, match=r"^idempotency_conflict$"):
            writes.record_outcome(
                conn, world["principal"], use_id, **(original | changed), idempotency_key=key
            )
    with psycopg.connect(world["admin"]) as conn:
        assert conn.execute("SELECT id FROM kb.outcome WHERE use_id=%s", (use_id,)).fetchall() == [
            (first["id"],)
        ]


@pytest.mark.parametrize("item_kind", ["source", "knowledge", "rule", "rule_draft"])
@pytest.mark.parametrize("dictionary_cursor", [False, True])
def test_issuance_records_exact_visible_item_without_creating_experience(
    world, item_kind, dictionary_cursor
):
    if item_kind == "rule_draft":
        item_kind = "rule"
        with psycopg.connect(world["dsn"]) as conn:
            ident = writes.create_rule(
                conn,
                world["principal"],
                world["lib"],
                writes.RuleInput(title="Private C23 synthetic draft"),
            )["id"]
    else:
        ident = (
            source(world)
            if item_kind == "source"
            else knowledge(world)
            if item_kind == "knowledge"
            else rule(world)["id"]
        )
    with psycopg.connect(world["dsn"]) as conn, transaction_identity(conn, world["principal"]):
        factory = dict_row if dictionary_cursor else None
        with conn.cursor(row_factory=factory) as cur:
            event = record_retrieval(cur, world["principal"], item_kind=item_kind, item_id=ident)
            assert isinstance(event, UUID)
    with psycopg.connect(world["admin"]) as conn:
        assert conn.execute(
            "SELECT principal_id,account_id,library_id,item_kind,item_id,version_no "
            "FROM kb.retrieval_event WHERE id=%s",
            (event,),
        ).fetchone() == (
            world["principal"].principal_id,
            world["principal"].account_id,
            world["lib"],
            item_kind,
            ident,
            1 if item_kind == "rule" else None,
        )
        assert conn.execute(
            "SELECT count(*) FROM kb.use_record WHERE principal_id=%s",
            (world["principal"].principal_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM kb.outcome WHERE reported_by=%s",
            (world["principal"].principal_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("denial", ["private_draft", "revoked", "foreign_account", "wrong_version"])
def test_issuance_denies_hidden_or_unmatched_item_without_audit_row(world, denial):
    principal, version = world["principal"], None
    if denial == "private_draft":
        ident, kind, principal = knowledge(world, published=False), "knowledge", world["other"]
        with psycopg.connect(world["admin"]) as conn:
            conn.execute(
                "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,'reader')",
                (world["lib"], principal.principal_id),
            )
    elif denial == "wrong_version":
        ident, kind, version = rule(world)["id"], "rule", 2
    else:
        ident, kind = source(world), "source"
        if denial == "foreign_account":
            principal = Principal(principal.principal_id, uuid4())
        else:
            with psycopg.connect(world["admin"]) as conn:
                conn.execute(
                    "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
                    (world["lib"], principal.principal_id),
                )
    with (
        psycopg.connect(world["dsn"]) as conn,
        transaction_identity(conn, principal),
        conn.cursor() as cur,
    ):
        with pytest.raises(AccessDenied):
            record_retrieval(cur, principal, item_kind=kind, item_id=ident, version_no=version)
    with psycopg.connect(world["admin"]) as conn:
        assert conn.execute(
            "SELECT count(*) FROM kb.retrieval_event WHERE item_id=%s", (ident,)
        ).fetchone() == (0,)
