"""C13 — integration fixtures. A real PostgreSQL 16.2, a real Procrastinate queue.

**A dedicated database.** The C06, C09 and C10 suites each apply their own
migration set in a session fixture, and re-applying is not available:
``CREATE POLICY`` and ``CREATE TRIGGER`` are not idempotent, so a second apply
aborts halfway with an error naming a policy rather than the real cause. C13
therefore creates its own database on the shared real server and applies
0001 → 0005 there once. One server, several schemas of state, no ordering
dependency: each suite owns its catalogue, and running any of them alone, or
all of them in one session, works.

**The queue schema is applied once too, and by the library that owns it.**
Procrastinate's ``schema.sql`` has no ``IF NOT EXISTS`` guards, so
``SchemaManager.apply_schema`` is a once-per-database operation. This card does
not copy that SQL, does not extend it, and does not write a second migration
path for it — ``kb.processing.jobs_queue.apply_queue_schema`` asks the library
to apply its own DDL, which is the only honest way to create it. The
guard below exists so a second call inside one session fails loudly rather
than half-creating objects.

**The data directory stays local.** Inherited from ``tests/conftest.py``: the
workspace is a NAS mount and ``initdb`` cannot lock there.

**Identity here is transport identity, not authentication.** There is no
Keycloak in this environment (E03 pending). What the tests actually exercise is
that the worker binds a configured service identity per transaction and takes
nothing from a message, so the stand-in costs the tests nothing.
"""

from __future__ import annotations

import contextlib
import pathlib
import uuid
from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

# this file is tests/integration/jobs/conftest.py, so parents[3] is ROOT
ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
C13_DB = "kb_c13"

# The account every principal in this harness belongs to. Not a principal.
ACCOUNT = UUID("00000000-0000-4000-8000-0000000000aa")

# A fixed worker service account. In a deployment this is a row in
# kb.library_grant, created by provisioning; here the world fixture makes it.
WORKER_PRINCIPAL = UUID("00000000-0000-4000-8000-0000000000bb")


@pytest.fixture(scope="session")
def c13_dsn(pg_server) -> str:
    """A dedicated catalogue on the shared real server. Created, never reused."""
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C13_DB,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C13_DB)))
    return make_conninfo(base, dbname=C13_DB)


@pytest.fixture(scope="session")
def schema(c13_dsn: str) -> bool:
    """Apply 0001 → 0005 ONCE per session. Structure only, no content."""
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c13_dsn, autocommit=True) as conn:
        conn.execute(script)
    return True


@pytest.fixture(scope="session")
def queue_app(c13_dsn: str, schema: bool, worker_pool: ConnectionPool, worker_identity):
    """The real Procrastinate application with the real worker tasks registered.

    Not a stand-in. ``Task.defer`` here really inserts into
    ``procrastinate_jobs`` and a real ``Worker`` really fetches from it. The
    task bodies are ``kb.processing.worker``'s, bound to the ``kb_worker``
    pool, so what a test runs is the code that would run in the container.
    """
    from kb.processing import jobs_queue
    from kb.processing.worker import build_worker

    app = jobs_queue.build_app(c13_dsn)
    app.open()
    try:
        with psycopg.connect(c13_dsn, autocommit=True) as conn:
            present = conn.execute(
                "SELECT 1 FROM pg_tables WHERE schemaname = current_schema() "
                "AND tablename = 'procrastinate_jobs'"
            ).fetchone()
        if present is None:
            jobs_queue.apply_queue_schema(app)
            _grant_queue_access(c13_dsn)
        _grant_worker_stage_access(c13_dsn)
        build_worker(app, worker_identity, connect=lambda: worker_pool.connection())
        yield app
    finally:
        app.close()


@pytest.fixture
async def worker_app(queue_app, c13_dsn: str):
    """The same application with the asynchronous connector a Worker needs.

    Function-scoped on purpose: the async pool belongs to the running event
    loop, and a session-scoped fixture would hand a pool to a loop that did not
    create it. Deferred and executed from one ``App``, so the tasks the worker
    runs are the very objects the submission side deferred.
    """
    from kb.processing import jobs_queue

    app = jobs_queue.build_worker_app(queue_app, c13_dsn)
    await app.open_async()
    try:
        yield app
    finally:
        await app.close_async()


def _grant_worker_stage_access(dsn: str) -> None:
    """Give the worker role what a stage needs on ``kb`` — in the test only.

    The same shape of gap as :func:`_grant_queue_access`, and found the same
    way: by running a stage. As 0002 and 0004 ship it, ``kb_worker`` may
    ``SELECT, UPDATE`` on ``kb.job`` and ``SELECT`` on ``kb.source`` and
    nothing else in the whole schema. A worker therefore cannot advance a
    source, cannot read the generation policy, and cannot create a job — the
    processing pipeline the card asks for is unreachable by the role that is
    supposed to run it.

    These two grants are what a stage needs and nothing more: the per-source
    processing checkpoint the parse stage writes, and read-only access to a
    table that holds two columns of policy and no user content. The real fix
    belongs to the single DDL owner and is reported as such in
    ``docs/handoff/results/C13.json`` (finding 2); applying it here is the
    migration-free arrangement the card allows, and it is the only reason the
    pipeline can be exercised at all.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("GRANT SELECT, UPDATE ON kb.source TO kb_worker")
        conn.execute("GRANT SELECT ON kb.generation_policy TO kb_worker")


def _grant_queue_access(dsn: str) -> None:
    """Give the runtime roles what they need on the queue — in the test only.

    This is a real gap, found by running it rather than by reading it. The
    transactional enqueue runs the caller\'s statement on the caller\'s
    connection, so the gateway\'s role must be able to execute
    ``procrastinate_defer_jobs_v1`` and write the queue rows; without these
    grants the first real submission fails with a bare "Database error".
    0002 grants the runtime roles on ``kb`` only, and Procrastinate ships its
    schema without granting anybody, so nobody has them.

    Fixing it properly is a change to the single DDL owner, and C13 reports it
    as such (``docs/handoff/results/C13.json``, finding 1). Applying it here,
    in fixture setup rather than in ``migrations/``, is the migration-free
    arrangement the card allows, and it is the only reason the transactional
    enqueue can be exercised at all. The grants are no wider than the code
    needs: the gateway enqueues and reads the checkpoint, the worker executes
    and reads the checkpoint.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("GRANT USAGE ON SCHEMA public TO kb_app, kb_worker")
        conn.execute(
            "GRANT EXECUTE ON FUNCTION procrastinate_defer_jobs_v1("
            "procrastinate_job_to_defer_v1[]) TO kb_app, kb_worker"
        )
        conn.execute("GRANT SELECT, INSERT, UPDATE ON procrastinate_jobs TO kb_app, kb_worker")
        conn.execute("GRANT SELECT, INSERT ON procrastinate_events TO kb_app, kb_worker")
        conn.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kb_app, kb_worker")


@pytest.fixture(scope="session")
def admin(c13_dsn: str, schema: bool) -> Iterator[psycopg.Connection]:
    """The migration superuser, used only to arrange rows.

    Seeding as the owner is how a library nobody can read gets created at all:
    the creator always becomes its manager, so the product's own API cannot
    produce a closed library. Every assertion about what a *caller* may do
    still goes through ``app.principal`` as ``kb_app`` or ``kb_worker``.
    """
    with psycopg.connect(c13_dsn, autocommit=True) as conn:
        yield conn


def _pool(dsn: str, role: str) -> ConnectionPool:
    # Never connect as the superuser here. A superuser BYPASSES RLS, so every
    # access assertion in this suite would pass vacuously.
    pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=6,
        open=True,
        kwargs={"autocommit": False, "options": f"-c role={role}"},
    )
    pool.wait(timeout=30)
    return pool


@pytest.fixture(scope="session")
def gateway_pool(c13_dsn: str, schema: bool) -> Iterator[ConnectionPool]:
    """``kb_app``: the role the gateway runs as. Submits jobs, reads status."""
    pool = _pool(c13_dsn, "kb_app")
    yield pool
    pool.close()


@pytest.fixture(scope="session")
def worker_pool(c13_dsn: str, schema: bool) -> Iterator[ConnectionPool]:
    """``kb_worker``: the narrower role the queue worker runs as.

    0002 grants it ``SELECT, UPDATE`` on ``kb.job`` and deliberately not
    ``INSERT``, so a worker cannot invent work. ``test_a_worker_cannot_create_a_job``
    is the test that keeps that honest.
    """
    pool = _pool(c13_dsn, "kb_worker")
    yield pool
    pool.close()


class World:
    """Arranges a scenario as the migration role."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._org: UUID | None = None

    def organisation(self) -> UUID:
        if self._org is None:
            self._org = uuid.uuid4()
            self.conn.execute(
                "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
                (self._org, f"c13-org-{self._org.hex[:8]}"),
            )
        return self._org

    def library(
        self,
        *,
        name: str | None = None,
        kind: str = "reference",
        grants: dict[UUID, str] | None = None,
    ) -> UUID:
        lib = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind) VALUES (%s, %s, %s, %s)",
            (lib, self.organisation(), name or f"c13-lib-{lib.hex[:8]}", kind),
        )
        for principal_id, role in (grants or {}).items():
            self.grant(lib, principal_id, role)
        return lib

    def grant(self, library_id: UUID, principal_id: UUID, role: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES (%s, %s, %s)",
            (library_id, principal_id, role),
        )

    def source(self, library_id: UUID, *, media_type: str = "text/plain", title: str = "s") -> UUID:
        src = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.source (id, library_id, title, media_type, submitted_by, "
            "object_key, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                src,
                library_id,
                f"{title}-{src.hex[:8]}",
                media_type,
                WORKER_PRINCIPAL,
                f"c13/{src.hex}",
                "b" * 64,
            ),
        )
        return src

    def source_status(self, source_id: UUID) -> str:
        row = self.conn.execute(
            "SELECT processing FROM kb.source WHERE id = %s", (source_id,)
        ).fetchone()
        return str(row[0]) if row else ""

    def source_count(self, library_id: UUID, status: str | None = None) -> int:
        if status is None:
            sql_text = "SELECT count(*) FROM kb.source WHERE library_id = %s"
            params: tuple = (library_id,)
        else:
            sql_text = "SELECT count(*) FROM kb.source WHERE library_id = %s AND processing = %s"
            params = (library_id, status)
        return int(self.conn.execute(sql_text, params).fetchone()[0])

    def job_count(self, library_id: UUID) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM kb.job WHERE library_id = %s", (library_id,)
            ).fetchone()[0]
        )

    def job_status(self, job_id: UUID) -> str:
        row = self.conn.execute("SELECT status FROM kb.job WHERE id = %s", (job_id,)).fetchone()
        return str(row[0]) if row else ""

    def index_generation_rows(self, library_id: UUID) -> int:
        return int(
            self.conn.execute(
                "SELECT count(*) FROM kb.index_generation WHERE library_id = %s", (library_id,)
            ).fetchone()[0]
        )

    def generation_policy(self, library_id: UUID, *, allowed: bool) -> None:
        self.conn.execute(
            "INSERT INTO kb.generation_policy (library_id, generation_allowed) VALUES (%s, %s) "
            "ON CONFLICT (library_id) DO UPDATE SET "
            "generation_allowed = EXCLUDED.generation_allowed",
            (library_id, allowed),
        )

    def queue_rows(self, job_id: UUID) -> list[tuple[str, str, int]]:
        cur = self.conn.execute(
            "SELECT task_name, status, attempts FROM procrastinate_jobs "
            "WHERE args ->> 'job_id' = %s ORDER BY id",
            (str(job_id),),
        )
        return [(str(a), str(b), int(c)) for a, b, c in cur.fetchall()]

    def queue_events(self, job_id: UUID) -> list[tuple[str, int]]:
        cur = self.conn.execute(
            "SELECT e.type, count(*) FROM procrastinate_events e "
            "JOIN procrastinate_jobs j ON j.id = e.job_id "
            "WHERE j.args ->> 'job_id' = %s GROUP BY e.type ORDER BY e.type",
            (str(job_id),),
        )
        return [(str(a), int(b)) for a, b in cur.fetchall()]


@pytest.fixture
def world(admin) -> World:
    return World(admin)


class Cast:
    """One test's principals, minted fresh so no test sees another's rows."""

    def __init__(self) -> None:
        self.owner = uuid.uuid4()
        self.contributor = uuid.uuid4()
        self.stranger = uuid.uuid4()
        self.account = ACCOUNT


@pytest.fixture
def cast() -> Cast:
    return Cast()


@pytest.fixture
def bound(gateway_pool: ConnectionPool):
    """A pooled connection with one principal bound for the whole block.

    Transaction-local, exactly as in the gateway, so a connection can never
    carry one caller's principal into the next call. Nothing in this suite
    connects as the superuser except :func:`world`.
    """
    from kb.access.policy import Principal, transaction_identity

    @contextlib.contextmanager
    def _bound(principal_id: UUID, pool: ConnectionPool | None = None):
        target = pool or gateway_pool
        with target.connection() as conn:
            with transaction_identity(
                conn, Principal(principal_id=principal_id, account_id=ACCOUNT)
            ):
                yield conn

    return _bound


@pytest.fixture
def run_as(gateway_pool: ConnectionPool, bound):
    """Run a callable on a pooled connection, as one principal, in one transaction."""

    def _run(principal_id: UUID, body, *, pool: ConnectionPool | None = None):
        with bound(principal_id, pool) as conn:
            return body(conn)

    return _run


@pytest.fixture(scope="session")
def account() -> UUID:
    return ACCOUNT


@pytest.fixture(scope="session")
def worker_principal() -> UUID:
    return WORKER_PRINCIPAL


@pytest.fixture(scope="session")
def worker_role() -> str:
    return WORKER_LIBRARY_ROLE


@pytest.fixture(scope="session")
def worker_identity():
    """The worker's service identity, as a deployment would configure it."""
    from kb.processing.worker import WorkerIdentity

    return WorkerIdentity(principal_id=WORKER_PRINCIPAL, account_id=ACCOUNT)


#: The role the worker service account needs on a library it processes.
#:
#: Not a preference. 0002's ``generation_policy_read`` requires curator, so a
#: worker holding only contributor cannot see whether generative stages are
#: enabled, and the cloud stage's quota gate would then always answer "not
#: enabled" — a worker silently refusing real work for lack of a grant rather
#: than because the owner said no. The grant is a ``kb.library_grant`` row, so
#: this is a provisioning decision rather than a schema change; who writes
#: those rows is C15's subject and is reported in
#: ``docs/handoff/results/C13.json``.
WORKER_LIBRARY_ROLE = "curator"


@pytest.fixture
def library(world: World, cast: Cast) -> UUID:
    """A library the worker and one contributor may both work on."""
    return world.library(
        grants={
            cast.contributor: "contributor",
            cast.owner: "manager",
            WORKER_PRINCIPAL: WORKER_LIBRARY_ROLE,
        }
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires a real PostgreSQL server")
