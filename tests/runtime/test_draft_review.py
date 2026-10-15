"""M01 — rejecting a draft, against a real PostgreSQL 16.2 and a real HTTP stack.

What this file proves, and where each guarantee lives:

* a curator withdraws a draft or a rule awaiting review, for both entities
  -> ``test_a_curator_withdraws_a_draft_or_a_rule_awaiting_review``
* the same call again returns the same result and writes nothing
  -> ``test_a_repeat_call_is_the_same_result_and_writes_nothing``
* write access is not curation, and the rest of write access still works
  -> ``test_write_access_is_not_curation``,
  ``test_a_contributor_is_refused_the_decision_and_not_the_library``
* curator of another library is not curator of this one
  -> ``test_a_curator_elsewhere_is_refused_here``
* a foreign id and an id that was never there answer the same way
  -> ``test_a_foreign_id_and_an_unknown_id_answer_the_same_way``
* a revoked or downgraded curator cannot replay the call
  -> ``test_a_revoked_curator_cannot_replay_the_call``,
  ``test_a_downgrade_stops_the_call``
* a published item is not withdrawn here
  -> ``test_a_published_item_is_not_withdrawn_here``
* the row, its provenance and its version history survive
  -> ``test_rejection_keeps_the_row_its_provenance_and_its_history``
* a withdrawn statement leaves the search index but not the library
  -> ``test_a_withdrawn_statement_leaves_the_search_index``

**The stand-in below is not authentication.** It reads a UUID from a test header
and puts a ``Principal`` on ``request.state``, which is what the real gateway's
``resolve_principal`` does *after* it has verified a signed Keycloak token. Every
assertion about who may see or change something still goes through ``kb_app``
against real RLS; what is under test is the rejection path, not the token check.
"""

from __future__ import annotations

import pathlib
import threading
from collections.abc import Callable
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

from kb.access.policy import Principal
from kb.catalog import content, writes
from kb.migrate import apply_migrations
from kb.runtime_identity import build_connection_factory

ROOT = pathlib.Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration

AUTH_HEADER = "X-Test-Auth-Subject"

#: Advisory lock used as the barrier in the concurrent-demotion test. The
#: demoting connection takes it first; the other connection parks on it, so the
#: interleaving is decided by the lock and never by a sleep.
GATE = 880_401

#: The two entities this endpoint exists for, in the same order everywhere.
BOTH = (True, False)
ENTITY_IDS = ["knowledge", "rule"]


# ------------------------------------------------------------------ database


@pytest.fixture(scope="module")
def draft_db(pg_server):
    """A database of our own, migrated once, on the session's real server.

    The migrations are not idempotent, so each module applies them to a fresh
    database rather than to a shared one.
    """
    name = "m01_reject_" + uuid4().hex[:12]
    admin = make_conninfo(pg_server.get_uri(), dbname=name)
    with psycopg.connect(pg_server.get_uri(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    assert apply_migrations(admin, ROOT / "migrations")
    assert apply_migrations(admin, ROOT / "migrations") == []
    return admin


@pytest.fixture(scope="module")
def runtime_dsn(draft_db):
    return make_conninfo(draft_db, user="kb_app")


@pytest.fixture(scope="module")
def pool(draft_db, runtime_dsn):
    """A pool connected as ``kb_app``, never as the migration superuser.

    A superuser bypasses RLS, so every access assertion in this file would pass
    vacuously on one. The gateway connects as ``kb_app`` for the same reason.
    """
    from kb.access.policy import clear_identity

    def _reset(conn: psycopg.Connection) -> None:
        conn.rollback()
        clear_identity(conn)
        conn.commit()

    opened = ConnectionPool(runtime_dsn, min_size=1, max_size=4, open=True, reset=_reset)
    opened.wait(timeout=30)
    yield opened
    opened.close()


# --------------------------------------------------------------------- world


class World:
    """Two libraries and a cast, arranged as the migration role.

    Arranging rows as the owner is the only way to make a library nobody holds a
    role on, and this class never answers a question about access: it only puts
    the rows in place that the questions are about.
    """

    def __init__(self, admin_dsn: str) -> None:
        self.admin_dsn = admin_dsn
        self.runtime_dsn = make_conninfo(admin_dsn, user="kb_app")
        self.account = uuid4()
        self.owner = uuid4()
        self.curator = uuid4()
        self.contributor = uuid4()
        self.reader = uuid4()
        self.stranger = uuid4()
        self.elsewhere = uuid4()
        self.lib = uuid4()
        self.other_lib = uuid4()
        self._arrange()

    def _arrange(self) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO kb.organisation(id,name) VALUES(%s,%s)",
                (self.account, f"m01-{self.account.hex[:8]}"),
            )
            for user in [
                self.owner,
                self.curator,
                self.contributor,
                self.reader,
                self.stranger,
                self.elsewhere,
            ]:
                conn.execute(
                    """INSERT INTO kb.membership(organisation_id,principal_id,issuer,subject,
              status) VALUES(%s,%s,'https://identity.test',%s,'active')""",
                    (self.account, user, str(user)),
                )
            for lib in (self.lib, self.other_lib):
                conn.execute(
                    "INSERT INTO kb.library(id,organisation_id,name,kind) "
                    "VALUES(%s,%s,%s,'reference')",
                    (lib, self.account, f"m01-{lib.hex[:8]}"),
                )
            for lib, who, role in [
                (self.lib, self.owner, "manager"),
                (self.lib, self.curator, "curator"),
                (self.lib, self.contributor, "contributor"),
                (self.lib, self.reader, "reader"),
                # a curator of another library, and only a reader of this one
                (self.lib, self.elsewhere, "reader"),
                (self.other_lib, self.owner, "manager"),
                (self.other_lib, self.elsewhere, "curator"),
            ]:
                conn.execute(
                    "INSERT INTO kb.library_grant(library_id,principal_id,role) VALUES(%s,%s,%s)",
                    (lib, who, role),
                )
            # the stranger is a member of the account and holds no role anywhere

    def principal(self, who: UUID) -> Principal:
        return Principal(principal_id=who, account_id=self.account)

    # ------------------------------------------------------------ arranging

    def knowledge(
        self, statement: str, *, library: UUID | None = None, created_by: UUID | None = None
    ) -> UUID:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            return conn.execute(
                "INSERT INTO kb.knowledge(library_id,statement,kind,created_by) "
                "VALUES(%s,%s,'assertion',%s) "
                "RETURNING id",
                (library or self.lib, statement, created_by),
            ).fetchone()[0]

    def rule(self, *, library: UUID | None = None, fragment: UUID | None = None) -> UUID:
        """A draft rule through the product's own path, so it is a real object."""
        with psycopg.connect(make_conninfo(self.admin_dsn, user="kb_app")) as conn:
            return writes.create_rule(
                conn,
                self.principal(self.owner),
                library or self.lib,
                writes.RuleInput(
                    title="Draft rule",
                    when_to_apply="Before a decision",
                    expected_effect="A recorded decision",
                    actions=["Record the decision"],
                    fragment_ids=[fragment] if fragment else [],
                ),
            )["id"]

    def published(self, ident: UUID, *, knowledge: bool) -> None:
        with psycopg.connect(make_conninfo(self.admin_dsn, user="kb_app")) as conn:
            writes.publish(conn, self.principal(self.owner), ident, knowledge=knowledge)

    def to_review(self, ident: UUID, *, knowledge: bool) -> None:
        """Put a row in ``needs_review`` — the other state a draft can be in."""
        table = sql.Identifier("knowledge" if knowledge else "rule")
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("UPDATE kb.{} SET publication='needs_review' WHERE id=%s").format(table),
                (ident,),
            )

    def source_with_fragment(self, text: str) -> tuple[UUID, UUID]:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            source = conn.execute(
                "INSERT INTO kb.source(library_id,title,media_type,submitted_by,object_key,"
                "content_hash) VALUES(%s,'Fixture','text/plain',%s,%s,%s) RETURNING id",
                (self.lib, self.owner, f"blobs/{uuid4().hex}", "0" * 64),
            ).fetchone()[0]
            fragment = conn.execute(
                "INSERT INTO kb.fragment(source_id,ordinal,text,locator_kind) "
                "VALUES(%s,0,%s,'none') RETURNING id",
                (source, text),
            ).fetchone()[0]
            return source, fragment

    def revoke(self, who: UUID) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
                (self.lib, who),
            )

    def set_role(self, who: UUID, role: str) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE kb.library_grant SET role=%s WHERE library_id=%s AND principal_id=%s",
                (role, self.lib, who),
            )

    def role_of(self, who: UUID) -> str | None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT role FROM kb.library_grant WHERE library_id=%s AND principal_id=%s",
                (self.lib, who),
            ).fetchone()
        return row[0] if row else None

    def deactivate(self, who: UUID) -> None:
        """Take the person out of the organisation, leaving their grant row.

        kb_app holds no DELETE on kb.membership and kb.deactivate_membership is
        the product's own way out of 'active', so the same state is arranged
        directly here: status plus the two columns the CHECK constraint demands.
        """
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE kb.membership SET status='deactivated', deactivated_at=now(), "
                "deactivated_by=%s WHERE organisation_id=%s AND principal_id=%s",
                (self.owner, self.account, who),
            )

    def remove_membership(self, who: UUID) -> None:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM kb.membership WHERE organisation_id=%s AND principal_id=%s",
                (self.account, who),
            )

    # ------------------------------------------------------------ observing

    def publication(self, ident: UUID, *, knowledge: bool) -> str:
        table = sql.Identifier("knowledge" if knowledge else "rule")
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            return conn.execute(
                sql.SQL("SELECT publication FROM kb.{} WHERE id=%s").format(table), (ident,)
            ).fetchone()[0]

    def write_version(self, ident: UUID, *, knowledge: bool) -> tuple:
        """``xmin`` is the row's write version, and it moves on every real UPDATE.

        It is how "the same result, and nothing was written a second time"
        becomes an observation rather than a claim.
        """
        table = sql.Identifier("knowledge" if knowledge else "rule")
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            return conn.execute(
                sql.SQL("SELECT xmin::text,library_id,created_at FROM kb.{} WHERE id=%s").format(
                    table
                ),
                (ident,),
            ).fetchone()

    def provenance(self, ident: UUID, *, knowledge: bool) -> list[tuple]:
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            if knowledge:
                return list(
                    conn.execute(
                        "SELECT knowledge_id,source_id,fragment_id FROM kb.knowledge_provenance "
                        "WHERE knowledge_id=%s ORDER BY source_id",
                        (ident,),
                    ).fetchall()
                )
            return list(
                conn.execute(
                    "SELECT rule_id,source_id,fragment_id FROM kb.rule_provenance "
                    "WHERE rule_id=%s ORDER BY source_id",
                    (ident,),
                ).fetchall()
            )


@pytest.fixture
def world(draft_db):
    return World(draft_db)


# ------------------------------------------------------------------------ api


def _build_app(pool: ConnectionPool, principal_for: Callable[[UUID], Principal]):
    """The real content router over a real pool, with a stand-in for Keycloak.

    ``principal_for`` is the world's own factory, so the Principal the endpoint
    sees is the same object the fixture describes — same account, same ids. A
    middleware that invented its own account would make every access assertion
    in this file describe a principal that does not exist in the database.
    """
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from kb.http import content as content_http

    app = FastAPI(title="m01-draft-review-under-test")
    # The factory, never the pool: the app must not be able to reach a
    # connection except through the one shared construction path.
    app.state.db_connection = build_connection_factory(pool)

    @app.middleware("http")
    async def stand_in_for_keycloak(request: Request, call_next):
        subject = request.headers.get(AUTH_HEADER)
        if subject is not None:
            try:
                resolved = UUID(subject)
            except ValueError:
                return JSONResponse({"detail": "malformed subject"}, status_code=401)
            request.state.principal = principal_for(resolved)
        return await call_next(request)

    app.include_router(content_http.router)
    return app


@pytest.fixture
def api(world, pool):
    """A real ASGI client. ``api("post", path, who)`` signs in as ``who``."""
    from fastapi.testclient import TestClient

    client = TestClient(_build_app(pool, world.principal))

    def _call(method: str, url: str, who: UUID | None = None):
        headers = {} if who is None else {AUTH_HEADER: str(who)}
        return getattr(client, method)(url, headers=headers)

    return _call


def reject(api, ident: UUID, *, knowledge: bool, who: UUID):
    path = ("knowledge" if knowledge else "rules") + f"/{ident}/reject"
    return api("post", "/" + path, who)


def draft(world: World, *, knowledge: bool) -> UUID:
    if knowledge:
        return world.knowledge("A statement awaiting a decision")
    return world.rule()


# ==================================================================== allowed


@pytest.mark.parametrize("knowledge", BOTH, ids=ENTITY_IDS)
@pytest.mark.parametrize("state", ["draft", "needs_review"], ids=["draft", "needs_review"])
def test_a_curator_withdraws_a_draft_or_a_rule_awaiting_review(world, api, knowledge, state):
    ident = draft(world, knowledge=knowledge)
    if state == "needs_review":
        world.to_review(ident, knowledge=knowledge)

    response = reject(api, ident, knowledge=knowledge, who=world.curator)

    assert response.status_code == 200, response.text
    assert response.json() == {"id": str(ident), "publication": "withdrawn"}
    assert world.publication(ident, knowledge=knowledge) == "withdrawn"


def test_a_repeat_call_is_the_same_result_and_writes_nothing(world, api):
    """Idempotent: the same answer again, and not a second write.

    Not a 409 either. A retry of an allowed call is a retry of an allowed
    call, and a conflict would tell the caller to do something about a state
    they had already asked for.
    """
    ident = world.knowledge("A statement about distribution channels")
    created = world.write_version(ident, knowledge=True)

    first = reject(api, ident, knowledge=True, who=world.curator)
    written = world.write_version(ident, knowledge=True)
    second = reject(api, ident, knowledge=True, who=world.curator)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert world.publication(ident, knowledge=True) == "withdrawn"
    # the first call wrote, the second one did not
    assert written[0] != created[0]
    assert world.write_version(ident, knowledge=True) == written


def test_a_repeat_call_after_a_different_actor_also_writes_nothing(world, api):
    """The second caller gets the same answer the first one got.

    A per-caller view — "you did not withdraw this" — would make a retry
    depend on who is asking, which is the opposite of idempotent.
    """
    ident = world.rule()
    world.set_role(world.contributor, "curator")

    assert reject(api, ident, knowledge=False, who=world.curator).json() == {
        "id": str(ident),
        "publication": "withdrawn",
    }
    replayed = reject(api, ident, knowledge=False, who=world.contributor)

    assert replayed.status_code == 200
    assert replayed.json() == {"id": str(ident), "publication": "withdrawn"}


# ==================================================================== refused


@pytest.mark.parametrize(
    "who_name", ["contributor", "reader", "stranger"], ids=["contributor", "reader", "stranger"]
)
def test_write_access_is_not_curation(world, api, who_name):
    """A contributor may edit a draft and still may not withdraw it."""
    who = getattr(world, who_name)
    for knowledge in BOTH:
        ident = draft(world, knowledge=knowledge)
        before = world.publication(ident, knowledge=knowledge)

        response = reject(api, ident, knowledge=knowledge, who=who)

        assert response.status_code == 403, (who_name, knowledge, response.text)
        assert world.publication(ident, knowledge=knowledge) == before


def test_a_contributor_is_refused_the_decision_and_not_the_library(world, runtime_dsn):
    """The refusal is about withdrawal, not about being locked out.

    A contributor can still create a draft rule, which is the whole point of
    the distinction: write access and curation are different things, and only
    the second one is being refused.
    """
    with psycopg.connect(runtime_dsn) as conn:
        created = writes.create_rule(
            conn,
            world.principal(world.contributor),
            world.lib,
            writes.RuleInput(
                title="A draft a contributor may write",
                when_to_apply="Before a decision",
                expected_effect="A recorded decision",
                actions=["Record the decision"],
            ),
        )
    assert created["publication"] == "draft"


def test_a_curator_elsewhere_is_refused_here(world, api):
    ident = world.knowledge("A statement in the wrong library")
    before = world.publication(ident, knowledge=True)

    response = reject(api, ident, knowledge=True, who=world.elsewhere)

    assert response.status_code == 403, response.text
    assert world.publication(ident, knowledge=True) == before


def test_a_foreign_id_and_an_unknown_id_answer_the_same_way(world, api):
    """One refusal for "not yours" and for "never was".

    The caller is a curator of ``lib``; the foreign rows are in ``other_lib``,
    which they cannot open, and the unknown ids were never written. If these
    answers differed by so much as a status code, the endpoint would be a
    directory of libraries this caller may not know about.
    """
    for knowledge in BOTH:
        foreign = draft(world, knowledge=knowledge)
        other = (
            world.knowledge("A statement in a library you cannot open", library=world.other_lib)
            if knowledge
            else world.rule(library=world.other_lib)
        )
        refused_foreign = reject(api, other, knowledge=knowledge, who=world.curator)
        refused_unknown = reject(api, uuid4(), knowledge=knowledge, who=world.curator)

        assert refused_foreign.status_code == refused_unknown.status_code == 403
        assert refused_foreign.json() == refused_unknown.json()
        assert world.publication(other, knowledge=knowledge) == "draft"
        assert world.publication(foreign, knowledge=knowledge) == "draft"

    # and one answer again for a caller who cannot open that library at all
    for knowledge in BOTH:
        other = (
            world.knowledge("A statement in a library at all", library=world.other_lib)
            if knowledge
            else world.rule(library=world.other_lib)
        )
        for who in (world.stranger, world.reader):
            assert reject(api, other, knowledge=knowledge, who=who).status_code == 403
            assert reject(api, uuid4(), knowledge=knowledge, who=who).status_code == 403


def test_a_revoked_curator_cannot_replay_the_call(world, api):
    """The grant is read again on every call, not remembered from the first one.

    Same principal, same library, and very likely the same pooled connection
    that served the successful call — so a role cached at the start of the
    request would still answer yes here.
    """
    ident = world.knowledge("A statement withdrawn while the curator was present")
    first = reject(api, ident, knowledge=True, who=world.curator)
    assert first.status_code == 200, first.text

    world.revoke(world.curator)
    replay = reject(api, ident, knowledge=True, who=world.curator)

    assert replay.status_code == 403, replay.text
    assert world.publication(ident, knowledge=True) == "withdrawn"


def test_a_downgrade_stops_the_call(world, api):
    """Curator, then contributor, between two calls by the same principal."""
    world.set_role(world.contributor, "curator")
    accepted = world.knowledge("A statement about who may decide")
    refused = world.knowledge("A second statement after the downgrade")
    assert reject(api, accepted, knowledge=True, who=world.contributor).status_code == 200

    world.set_role(world.contributor, "contributor")

    assert reject(api, refused, knowledge=True, who=world.contributor).status_code == 403
    assert world.publication(refused, knowledge=True) == "draft"


# ================================================================= membership
# A library_grant row says what a person may do in a library. kb.membership
# says whether they are still here at all. A grant row left behind by someone
# who has left is not permission, so the mutation tests for an active
# membership and not only for a role.
#
# The two cases below are defended differently, and the tests say which, because
# a control assertion written for one of them is wrong for the other.


def as_principal(conn: psycopg.Connection, principal: Principal) -> None:
    """Bind the transaction-local identity without owning the transaction.

    ``transaction_identity`` owns its transaction, so an explicit rollback
    inside it is an error. These tests need to take a write back afterwards, so
    they set the same two GUCs directly and own the transaction themselves.
    """
    conn.execute("SELECT set_config('app.principal', %s, true)", (str(principal.principal_id),))
    conn.execute("SELECT set_config('app.account', %s, true)", (str(principal.account_id),))


def test_a_deactivated_member_is_refused(world, api):
    """The grant survives the person, and the person is gone.

    ``kb.membership_allows`` is false while a deactivated row exists, and
    ``membership_must_be_active`` is a RESTRICTIVE policy on ``kb.library_grant``,
    which ``kb.effective_role`` reads — so the database already refuses this
    caller's bare write. The endpoint must answer 403 all the same, and it must
    agree with the database rather than discovering the row another way.
    """
    ident = world.knowledge("A statement a deactivated curator may not withdraw")
    world.deactivate(world.curator)
    assert world.role_of(world.curator) == "curator", "the grant row must still be there"

    with psycopg.connect(world.admin_dsn, autocommit=True) as conn:
        assert (
            conn.execute("SELECT kb.membership_allows(%s)", (world.curator,)).fetchone()[0] is False
        )
    with psycopg.connect(world.runtime_dsn) as conn:
        as_principal(conn, world.principal(world.curator))
        bare = conn.execute(
            "UPDATE kb.knowledge SET publication='withdrawn' WHERE id=%s RETURNING id", (ident,)
        )
        assert bare.rowcount == 0, "RLS already refuses a deactivated member"
        conn.rollback()

    response = reject(api, ident, knowledge=True, who=world.curator)

    assert response.status_code == 403, response.text
    assert world.publication(ident, knowledge=True) == "draft"


def test_a_grant_with_no_membership_row_at_all_is_refused(world, api):
    """The load-bearing membership case: the write RLS lets through.

    ``kb.membership_allows`` deliberately treats an *absent* membership row as
    not-a-block — a principal who never went through the invite lifecycle is not
    blocked by it. So the bare write below lands, and RLS is not what refuses
    this caller. The mutation's own requirement of an active membership row is.
    """
    ident = world.knowledge("A statement nobody can withdraw without a membership")
    world.remove_membership(world.curator)
    assert world.role_of(world.curator) == "curator", "the grant row must still be there"

    with psycopg.connect(world.runtime_dsn) as conn:
        as_principal(conn, world.principal(world.curator))
        bare = conn.execute(
            "UPDATE kb.knowledge SET publication='withdrawn' WHERE id=%s RETURNING id", (ident,)
        )
        assert bare.rowcount == 1, "RLS does not stop this caller; the endpoint must"
        conn.rollback()

    response = reject(api, ident, knowledge=True, who=world.curator)

    assert response.status_code == 403, response.text
    assert world.publication(ident, knowledge=True) == "draft"


def test_the_replay_path_re_checks_a_membership_that_is_gone(world, api):
    """Already withdrawn is not a free pass: authority is read again.

    The row is in the target state, so the only thing left to decide is whether
    this caller may still have put it there.
    """
    ident = world.knowledge("A statement withdrawn, then its curator departs")
    assert reject(api, ident, knowledge=True, who=world.curator).status_code == 200
    world.remove_membership(world.curator)

    replay = reject(api, ident, knowledge=True, who=world.curator)

    assert replay.status_code == 403, replay.text
    assert world.publication(ident, knowledge=True) == "withdrawn"


# ============================================================== concurrency


def test_a_demotion_committed_after_the_authority_read_cannot_ride_along(
    world, api, draft_db, runtime_dsn
):
    """The race this endpoint used to have, with two connections and a barrier.

    The old shape read the role in one statement and then issued a bare
    ``UPDATE ... WHERE id=%s`` in the next. Nothing locked ``kb.library_grant``
    between the two — the row lock was on the rule — so a demotion committed in
    that window was invisible to the read and visible to the write.

    The barrier is an advisory lock. The demoting connection takes it *before*
    the reader starts, so the reader is provably parked at
    ``pg_advisory_xact_lock`` when the demotion commits; no sleep is involved
    and the order does not depend on timing. The reader then issues the bare
    write and it lands, which is the hazard, reproduced on demand.
    """
    ident = world.knowledge(
        "A statement that races its own curator's demotion", created_by=world.curator
    )
    caller = world.principal(world.curator)
    read_rank: list[int] = []
    bare_writes: list[int] = []
    read_done = threading.Event()

    def reader() -> None:
        with psycopg.connect(runtime_dsn) as conn:
            as_principal(conn, caller)
            # A parked test must fail, not hang.
            conn.execute("SET LOCAL lock_timeout = '15s'")
            cur = conn.cursor()
            # 1. the authority read, exactly as the old code did it
            cur.execute(
                "SELECT COALESCE(kb.role_rank(kb.effective_role(%s,%s)),0)",
                (caller.principal_id, world.lib),
            )
            read_rank.append(cur.fetchone()[0])
            read_done.set()  # the read is done, and happened before any demotion
            # 2. park until the demotion is committed
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (GATE,))
            # 3. the write the old code issued next: no authority in the statement
            cur.execute(
                "UPDATE kb.knowledge SET publication='withdrawn' WHERE id=%s RETURNING id",
                (ident,),
            )
            bare_writes.append(cur.rowcount)
            conn.rollback()  # the hazard write is observed, then taken back

    with psycopg.connect(draft_db) as demoter:
        # Taken *before* the reader starts, so the reader cannot get past the
        # gate and commit its write until the demotion below is committed.
        demoter.execute("SELECT pg_advisory_lock(%s)", (GATE,))
        thread = threading.Thread(target=reader, name="m01-demotion-reader")
        thread.start()
        assert read_done.wait(timeout=30), "the reader never finished its authority read"
        demoter.execute(
            "UPDATE kb.library_grant SET role='contributor' "
            "WHERE library_id=%s AND principal_id=%s",
            (world.lib, world.curator),
        )
        demoter.commit()
        demoter.execute("SELECT pg_advisory_unlock(%s)", (GATE,))
    thread.join(timeout=60)
    assert not thread.is_alive(), "the parked reader never woke up"

    # The reader read curator *before* the demotion was committed and wrote
    # *after* it. That pair is the proof the interleaving really happened.
    assert read_rank == [30], "the reader must have seen curator, not contributor"
    assert world.role_of(world.curator) == "contributor", "the demotion must have committed"
    assert bare_writes == [1], "an unguarded write rides along on a stale decision"
    assert world.publication(ident, knowledge=True) == "draft", "rolled back"

    # The endpoint, in exactly that state, refuses and leaves the draft alone.
    response = reject(api, ident, knowledge=True, who=world.curator)

    assert response.status_code == 403, response.text
    assert world.publication(ident, knowledge=True) == "draft"


def test_a_demotion_that_commits_after_the_call_does_not_undo_it(world, api, draft_db):
    """The honest boundary of the fix: one statement, one snapshot.

    A demotion that is still uncommitted when the statement runs is invisible to
    it, the withdrawal lands, and the very next call is refused. That is not a
    hole in the check — it is what one statement under one snapshot means — and
    it is pinned here so the behaviour is a decision rather than a surprise.
    """
    ident = world.knowledge("A statement withdrawn before the demotion commits")
    with psycopg.connect(draft_db) as demoter:
        demoter.execute(
            "UPDATE kb.library_grant SET role='contributor' "
            "WHERE library_id=%s AND principal_id=%s",
            (world.lib, world.curator),
        )
        # uncommitted: another session still reads curator
        assert reject(api, ident, knowledge=True, who=world.curator).status_code == 200
        demoter.commit()

    assert reject(api, ident, knowledge=True, who=world.curator).status_code == 403
    assert world.publication(ident, knowledge=True) == "withdrawn"


# ============================================================== other states


@pytest.mark.parametrize("knowledge", BOTH, ids=ENTITY_IDS)
def test_a_published_item_is_not_withdrawn_here(world, api, knowledge):
    """This endpoint does not unpublish, and it does not pretend to.

    A 200 here would be a false success: the caller would believe something
    had changed that had not.
    """
    ident = draft(world, knowledge=knowledge)
    world.published(ident, knowledge=knowledge)
    before = world.write_version(ident, knowledge=knowledge)

    response = reject(api, ident, knowledge=knowledge, who=world.curator)

    assert response.status_code == 400, response.text
    assert world.publication(ident, knowledge=knowledge) == "published"
    assert world.write_version(ident, knowledge=knowledge) == before


def test_rejection_keeps_the_row_its_provenance_and_its_history(world, api, runtime_dsn):
    """Rejection is a change of state, not a deletion and not a re-versioning."""
    source, fragment = world.source_with_fragment("The source says something about pricing")
    withdrawn_rule = None
    for knowledge in BOTH:
        if knowledge:
            ident = world.knowledge("A statement traced to a fragment")
            with psycopg.connect(world.admin_dsn, autocommit=True) as conn:
                conn.execute(
                    "INSERT INTO kb.knowledge_provenance(knowledge_id,source_id,fragment_id,"
                    "author) VALUES(%s,%s,%s,'An Author')",
                    (ident, source, fragment),
                )
        else:
            ident = withdrawn_rule = world.rule(fragment=fragment)
        before = world.write_version(ident, knowledge=knowledge)
        provenance_before = world.provenance(ident, knowledge=knowledge)
        assert provenance_before, "the fixture must have provenance to preserve"

        assert reject(api, ident, knowledge=knowledge, who=world.curator).status_code == 200

        assert world.publication(ident, knowledge=knowledge) == "withdrawn"
        assert world.provenance(ident, knowledge=knowledge) == provenance_before
        # the row is still the row it was: same library, same creation instant
        after = world.write_version(ident, knowledge=knowledge)
        assert after[1:] == before[1:]
        assert after[0] != before[0], "a withdrawal is a write, unlike a repeat call"

    with psycopg.connect(world.admin_dsn, autocommit=True) as conn:
        first = conn.execute(
            "SELECT id,version_no,lineage_id,publication FROM kb.rule WHERE id=%s",
            (withdrawn_rule,),
        ).fetchone()
    assert first[1:] == (1, first[2], "withdrawn")
    with psycopg.connect(runtime_dsn) as conn:
        second = writes.create_rule(
            conn,
            world.principal(world.owner),
            world.lib,
            writes.RuleInput(
                title="A correction",
                when_to_apply="After the withdrawal",
                expected_effect="The corrected rule",
                actions=["Correct it"],
            ),
            replaces=withdrawn_rule,
        )
    assert second["version_no"] == 2 and second["lineage_id"] == withdrawn_rule


def test_a_withdrawn_statement_leaves_the_search_index_but_not_the_library(world, api, runtime_dsn):
    """Curator search stops serving it; curation retains the row, readers see no draft."""
    ident = world.knowledge("segmentation determines budget allocation")
    reader = world.principal(world.curator)

    def found() -> list[str]:
        with psycopg.connect(runtime_dsn) as conn:
            hits = content.search_knowledge(conn, reader, "segmentation", [world.lib])["hits"]
        return [str(hit["id"]) for hit in hits]

    assert found() == [str(ident)]
    assert reject(api, ident, knowledge=True, who=world.curator).status_code == 200
    assert found() == []

    with psycopg.connect(runtime_dsn) as conn:
        listed = writes.list_knowledge(conn, reader, world.lib)
        assert writes.list_knowledge(conn, world.principal(world.reader), world.lib) == []
    assert [(row["id"], row["publication"]) for row in listed] == [(ident, "withdrawn")]
