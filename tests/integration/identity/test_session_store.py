"""C07 — session and login state are in PostgreSQL, not in a gateway.

Real PostgreSQL 16.2, real DDL from ``migrations/0003_identity_sessions.sql``,
real ``kb_app`` — the unprivileged runtime role, not the table owner. A test
that ran the store as the owner would prove nothing about the privilege model.

The property under test throughout is SCALING.md S01: a browser alternates
between two gateways, one of them restarts, and the session is unaffected. The
way this file demonstrates it is not by assertion alone — every store built
here is a separate object with its own connections and its own instance id,
and "the first gateway is gone" is expressed by simply never calling it again.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from kb.access.identity import VerifiedIdentity
from kb.access.policy import ROLE_RANK, transaction_identity
from kb.access.session import (
    LoginTransactionInvalid,
    Sealer,
    SessionIdentityIncomplete,
    SessionMissing,
    SessionStore,
    code_challenge_for,
    csrf_matches,
    new_browser_binding,
    new_code_verifier,
    new_state,
)
from kb.contracts.enums import LibraryRole

pytestmark = pytest.mark.integration

UTC = dt.UTC
NOW = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
ISSUER = "http://127.0.0.1:65535/realms/knowledge-hub"
JWT_SHAPED = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.")


def principal_id() -> uuid.UUID:
    return uuid.uuid4()


def identity(
    subject: str | None = None,
    account_id: str | None = None,
    *,
    issuer: str = ISSUER,
    lifetime_seconds: int = 3600,
) -> VerifiedIdentity:
    subject = subject or str(principal_id())
    return VerifiedIdentity(
        issuer=issuer,
        subject=subject,
        principal_id=uuid.UUID(subject),
        account_id=uuid.UUID(account_id) if account_id else None,
        scopes=frozenset({"openid"}),
        issued_at=NOW,
        expires_at=NOW + dt.timedelta(seconds=lifetime_seconds),
        provider_session_id=f"kc-session-{subject}",
    )


def admin(identity_db: str):
    """A connection with table-owner rights, for arranging fixtures only."""
    return psycopg.connect(identity_db, autocommit=True, row_factory=dict_row)


def open_login(store: SessionStore, **overrides):
    state = overrides.pop("state", new_state())
    binding = overrides.pop("binding", new_browser_binding())
    verifier = overrides.pop("code_verifier", new_code_verifier())
    store.open_login(
        state=state,
        binding=binding,
        code_verifier=verifier,
        code_challenge=code_challenge_for(verifier),
        redirect_uri=overrides.pop("redirect_uri", "https://kb.invalid/auth/callback"),
        return_to=overrides.pop("return_to", "/libraries"),
        nonce=overrides.pop("nonce", "c07-nonce"),
        now=overrides.pop("now", NOW),
    )
    return state, binding, verifier


# ------------------------------------------------- the pending OAuth transaction


def test_a_login_started_on_one_gateway_completes_on_another(make_store):
    """The state and the PKCE verifier are rows, not process state. A callback
    that lands on a different gateway — or on the same one after a restart —
    finds everything it needs."""
    started = make_store("gateway-a")
    other = make_store("gateway-b")
    state, binding, verifier = open_login(started)

    transaction = other.consume_login(state, binding=binding, now=NOW + dt.timedelta(minutes=1))

    assert transaction.code_verifier == verifier
    assert transaction.code_challenge == code_challenge_for(verifier)
    assert transaction.return_to == "/libraries"
    assert transaction.nonce == "c07-nonce"


def test_the_state_parameter_cannot_be_replayed(make_store):
    """An intercepted callback cannot be redeemed twice: the claim is an
    UPDATE ... WHERE consumed_at IS NULL, so the second attempt matches
    nothing."""
    store = make_store("gateway-a")
    state, binding, _ = open_login(store)
    store.consume_login(state, binding=binding, now=NOW)
    with pytest.raises(LoginTransactionInvalid):
        store.consume_login(state, binding=binding, now=NOW)


def test_a_callback_in_the_wrong_browser_is_refused(make_store):
    """`state` alone stops a forged callback, but an attacker who *starts* a
    login knows their own state. The callback must also arrive in the browser
    that was handed the binding cookie."""
    store = make_store("gateway-a")
    state, _binding, _verifier = open_login(store)

    with pytest.raises(LoginTransactionInvalid):
        store.consume_login(state, binding=new_browser_binding(), now=NOW)
    with pytest.raises(LoginTransactionInvalid):
        store.consume_login(state, binding=None, now=NOW)

    # and the row is still unconsumed, so the rightful browser can finish
    assert store.consume_login(state, binding=_binding, now=NOW).return_to == "/libraries"


def test_an_expired_login_transaction_is_refused(make_store):
    store = make_store("gateway-a")
    state, binding, _ = open_login(store)
    with pytest.raises(LoginTransactionInvalid):
        store.consume_login(state, binding=binding, now=NOW + dt.timedelta(hours=1))


def test_an_unknown_state_is_refused(make_store):
    with pytest.raises(LoginTransactionInvalid):
        make_store("gateway-a").consume_login(new_state(), binding="x", now=NOW)


def test_the_pkce_verifier_is_not_stored_in_the_clear(identity_db, make_store):
    store = make_store("gateway-a")
    _state, _binding, verifier = open_login(store)
    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT code_verifier_sealed, state_hash, binding_hash FROM kb.auth_login_transaction"
        )
        rows = cur.fetchall()
    assert rows
    for row in rows:
        assert verifier not in row["code_verifier_sealed"]
        assert row["state_hash"] is not None
        assert JWT_SHAPED.search(str(row)) is None
    # the browser never sees the sealed blob either: the value that goes into
    # the authorization request is the challenge, not the verifier
    assert verifier not in code_challenge_for(verifier)


def test_a_wrong_key_cannot_unseal_a_pending_login(identity_db, make_store, sealer):
    store = make_store("gateway-a")
    state, binding, _ = open_login(store)
    impostor = Sealer(Sealer.generate())
    with pytest.raises(SessionIdentityIncomplete):
        impostor.unseal(_sealed_value(identity_db))
    assert store.consume_login(state, binding=binding, now=NOW) is not None


def _sealed_value(identity_db: str) -> str:
    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT code_verifier_sealed FROM kb.auth_login_transaction LIMIT 1")
        return cur.fetchone()["code_verifier_sealed"]


# ----------------------------------------------------------------- sessions


def test_a_session_survives_the_gateway_that_created_it(make_store):
    """S01. Gateway A issues the session, is then abandoned entirely, and
    gateway B — a different object with its own connections and a different
    instance id — serves the next request from the same cookie."""
    gateway_a = make_store("gateway-a")
    issued = gateway_a.create_session(identity(account_id=str(uuid.uuid4())), now=NOW)
    del gateway_a  # the process is gone; nothing in RAM survives it

    gateway_b = make_store("gateway-b")
    session = gateway_b.load(issued.cookie_value, now=NOW + dt.timedelta(minutes=1))

    assert session.principal_id == issued.session.principal_id
    assert session.account_id == issued.session.account_id
    assert session.id == issued.session.id


def test_the_database_records_which_gateway_served_the_session(identity_db, make_store):
    """The evidence is in the row, not only in the assertion above."""
    make_store("gateway-a").create_session(identity(), now=NOW)
    issued = make_store("gateway-a").create_session(identity(), now=NOW)
    make_store("gateway-b").load(issued.cookie_value, now=NOW + dt.timedelta(minutes=1))

    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT created_by_instance, last_served_by_instance FROM kb.browser_session "
            "WHERE id = %(id)s",
            {"id": issued.session.id},
        )
        row = cur.fetchone()
    assert row["created_by_instance"] == "gateway-a"
    assert row["last_served_by_instance"] == "gateway-b"


def test_a_forged_cookie_is_refused(make_store):
    store = make_store("gateway-a")
    issued = store.create_session(identity(), now=NOW)
    session_id, _secret = issued.cookie_value.split(".")

    for bad in (
        f"{session_id}.{'0' * 43}",  # right id, wrong secret
        f"{uuid.uuid4()}.{'0' * 43}",  # wrong id
        "no-dot-at-all",
        "",
        None,
        f"{session_id}.",
        f"not-a-uuid.{'0' * 43}",
    ):
        with pytest.raises(SessionMissing):
            store.load(bad, now=NOW + dt.timedelta(minutes=1))


def test_a_session_row_cannot_be_turned_back_into_a_cookie(identity_db, make_store):
    """Only the digest is stored, so a database dump yields no usable cookie."""
    issued = make_store("gateway-a").create_session(identity(), now=NOW)
    _session_id, secret = issued.cookie_value.split(".")
    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT row_to_json(s)::text AS row FROM kb.browser_session s WHERE id = %(id)s",
            {"id": issued.session.id},
        )
        dumped = cur.fetchone()["row"]
    assert secret not in dumped
    assert issued.cookie_value not in dumped
    assert JWT_SHAPED.search(dumped) is None


def test_no_token_of_any_kind_is_stored(identity_db, make_store):
    """A session is a credential, not a token cache: the row must not contain
    anything replayable against another service."""
    store = make_store("gateway-a")
    issued = store.create_session(identity(), now=NOW)
    state, binding, _ = open_login(store)
    store.consume_login(state, binding=binding, now=NOW)

    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'kb' "
            "AND table_name IN ('browser_session', 'auth_login_transaction')"
        )
        columns = {row["column_name"] for row in cur.fetchall()}
    assert issued.session.id is not None
    for forbidden in ("access_token", "refresh_token", "id_token", "token", "claims"):
        assert forbidden not in columns, columns


def test_a_session_without_an_account_never_becomes_a_principal(make_store):
    """Unknown is None. A placeholder account would be a tenant the database
    has never heard of, and every ACL decision after it would be made against
    that fiction."""
    issued = make_store("gateway-a").create_session(identity(account_id=None), now=NOW)
    session = make_store("gateway-b").load(issued.cookie_value, now=NOW + dt.timedelta(minutes=1))
    assert session.account_id is None
    with pytest.raises(SessionIdentityIncomplete):
        session.to_principal()


def test_a_session_becomes_the_c06_principal(make_store):
    account = uuid.uuid4()
    issued = make_store("gateway-a").create_session(identity(account_id=str(account)), now=NOW)
    principal = issued.session.to_principal()
    assert principal.principal_id == issued.session.principal_id
    assert principal.account_id == account
    assert principal.generation_watermark >= 1  # C06 refuses a watermark below 1


def test_a_revoked_session_stops_working_immediately(make_store):
    store = make_store("gateway-a")
    other = make_store("gateway-b")
    issued = store.create_session(identity(), now=NOW)
    assert other.load(issued.cookie_value, now=NOW) is not None

    assert store.revoke(issued.session.id, reason="logout", now=NOW) is True

    with pytest.raises(SessionMissing):
        other.load(issued.cookie_value, now=NOW + dt.timedelta(minutes=1))
    # a second revocation is a no-op, not an error
    assert store.revoke(issued.session.id, reason="logout", now=NOW) is False


def test_the_idle_window_closes(make_store):
    store = make_store("gateway-a", idle_minutes=15)
    issued = store.create_session(identity(), now=NOW)
    # 14 minutes in, the session is live and that use pushes the deadline out
    live = store.load(issued.cookie_value, now=NOW + dt.timedelta(minutes=14))
    assert live is not None
    assert live.idle_expires_at == NOW + dt.timedelta(minutes=29)
    with pytest.raises(SessionMissing):
        store.load(issued.cookie_value, now=NOW + dt.timedelta(minutes=31))

    # a session nobody touches dies on its original schedule
    untouched = store.create_session(identity(), now=NOW)
    with pytest.raises(SessionMissing):
        store.load(untouched.cookie_value, now=NOW + dt.timedelta(minutes=16))


def test_activity_extends_the_idle_window_but_not_the_absolute_deadline(make_store):
    store = make_store("gateway-a", idle_minutes=15, absolute_hours=1)
    issued = store.create_session(identity(), now=NOW)

    still_working = store.load(issued.cookie_value, now=NOW + dt.timedelta(minutes=14))
    assert still_working is not None
    assert still_working.idle_expires_at > still_working.created_at

    # an hour of continuous use does not buy another hour
    with pytest.raises(SessionMissing):
        store.load(issued.cookie_value, now=NOW + dt.timedelta(hours=2))


def test_purge_keeps_recent_revocations_for_support(identity_db, make_store):
    # A long absolute window, so the only thing that can delete this row is
    # the revocation-retention rule.
    store = make_store("gateway-a", absolute_hours=24 * 30)
    issued = store.create_session(identity(), now=NOW)
    store.revoke(issued.session.id, reason="logout", now=NOW)

    store.purge(now=NOW + dt.timedelta(days=1))
    assert _session_row(identity_db, issued.session.id) is not None

    store.purge(now=NOW + dt.timedelta(days=30))
    assert _session_row(identity_db, issued.session.id) is None


def test_purge_removes_a_session_whose_absolute_deadline_passed(identity_db, make_store):
    store = make_store("gateway-a", absolute_hours=1)
    issued = store.create_session(identity(), now=NOW)
    store.purge(now=NOW + dt.timedelta(minutes=59))
    assert _session_row(identity_db, issued.session.id) is not None
    store.purge(now=NOW + dt.timedelta(hours=2))
    assert _session_row(identity_db, issued.session.id) is None


def _session_row(identity_db: str, session_id: uuid.UUID) -> dict | None:
    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT revoked_at FROM kb.browser_session WHERE id = %(id)s",
            {"id": session_id},
        )
        return cur.fetchone()


# ------------------------------------------------------------------- CSRF


def test_csrf_matching(make_store):
    store = make_store("gateway-a")
    issued = store.create_session(identity(), now=NOW)
    session = issued.session

    assert csrf_matches(session, issued.csrf_token, issued.csrf_token) is True
    assert csrf_matches(session, None, issued.csrf_token) is False
    assert csrf_matches(session, issued.csrf_token, None) is False
    assert csrf_matches(session, "attacker", "attacker") is False


def test_a_csrf_token_from_another_session_does_not_work(make_store):
    store = make_store("gateway-a")
    first = store.create_session(identity(), now=NOW)
    second = store.create_session(identity(), now=NOW)
    # the attacker controls both halves of their own pair...
    assert csrf_matches(second.session, "attacker", "attacker") is False
    # ...and a real token from another session is equally useless here
    assert csrf_matches(second.session, first.csrf_token, first.csrf_token) is False
    assert csrf_matches(first.session, second.csrf_token, second.csrf_token) is False


# ------------------------------------------------------------- privileges


def test_the_runtime_role_can_reach_sessions_and_the_worker_cannot(identity_db, make_store):
    make_store("gateway-a").create_session(identity(), now=NOW)
    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute("SET ROLE kb_app")
        cur.execute("SELECT count(*) AS n FROM kb.browser_session")
        assert cur.fetchone()["n"] >= 1
        cur.execute("SET ROLE kb_worker")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("SELECT count(*) FROM kb.browser_session")


def test_sessions_are_not_world_readable(identity_db):
    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('kb_worker', 'kb.browser_session', 'SELECT') AS worker, "
            "       has_table_privilege('kb_app', 'kb.browser_session', 'SELECT') AS app"
        )
        row = cur.fetchone()
    assert row["worker"] is False
    assert row["app"] is True


# --------------------------------------------- identity meets the access model


def test_a_session_principal_is_what_the_rls_policies_see(identity_db, make_store):
    """C07 and C06 meet here. The identity that comes out of a verified token
    is the identity that opens the rows, through the transaction-local GUC —
    and only for the length of the transaction."""
    person, account = str(uuid.uuid4()), uuid.uuid4()
    library = uuid.uuid4()
    with admin(identity_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kb.organisation (id, name) VALUES (%(id)s, 'c07-org')",
            {"id": account},
        )
        cur.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind) "
            "VALUES (%(id)s, %(org)s, 'c07-lib', 'reference')",
            {"id": library, "org": account},
        )
        cur.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) "
            "VALUES (%(lib)s, %(who)s, 'contributor')",
            {"lib": library, "who": person},
        )

    issued = make_store("gateway-a").create_session(
        identity(subject=person, account_id=str(account)), now=NOW
    )
    session = make_store("gateway-b").load(issued.cookie_value, now=NOW + dt.timedelta(minutes=1))
    principal = session.to_principal()

    with psycopg.connect(identity_db, row_factory=dict_row) as conn:
        conn.execute("SET ROLE kb_app")
        # end the implicit transaction so that transaction_identity below opens
        # a real top-level one; a savepoint would keep set_config(..., true)
        # alive after the block, which is exactly the pooling hazard C06 warns
        # about.
        conn.commit()
        with transaction_identity(conn, principal) as scoped:
            with scoped.cursor() as cur:
                cur.execute("SELECT library_id, role FROM kb.library_grant")
                grants = cur.fetchall()
        assert [row["library_id"] for row in grants] == [library]
        assert ROLE_RANK[LibraryRole(grants[0]["role"])] == ROLE_RANK[LibraryRole.CONTRIBUTOR]

        # the GUC is transaction-local: the pooled connection is anonymous again
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.principal', true) AS who")
            assert cur.fetchone()["who"] in {"", None}
            cur.execute("SELECT count(*) AS n FROM kb.library_grant")
            assert cur.fetchone()["n"] == 0
