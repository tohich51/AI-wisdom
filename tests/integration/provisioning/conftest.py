"""C15 — integration fixtures. A real PostgreSQL 16.2, real DDL, real RLS.

What is real here and what is not, before anything else:

* **Real**: the server (pgserver ships PostgreSQL binaries), the DDL, the
  roles, the RLS, every policy and every constraint in
  ``migrations/0004_provisioning.sql``. A test that says "the gateway cannot
  create an account" is asserting on PostgreSQL's own refusal, not on a
  Python branch.
* **Not real**: OpenViking. It is not installed, it cannot be installed here
  (no embedding provider: Ollama is absent and a paid API is forbidden), and
  nothing in this directory pretends otherwise. The tests that exercise the
  *orchestration* use the doubles in ``doubles.py``, which say in their own
  docstring that they are not OpenViking, and the results are reported as
  "the policy is verified, the remote effect is not".

**A dedicated database.** The C06 and C09 suites each apply every
``migrations/0*.sql`` in a session fixture, and those files are not
idempotent — ``CREATE POLICY`` and ``CREATE TRIGGER`` abort halfway on a
second application. So this suite applies the same DDL to its own database on
the same real server, exactly as ``tests/integration/libraries/conftest.py``
does. One server, three catalogues, no ordering dependency.

**The data directory stays local.** Inherited from ``tests/conftest.py``: the
workspace is a NAS mount and ``initdb`` cannot lock there.

Connections come in three flavours, and the difference matters:

* ``admin`` — the migration role. It arranges rows the product's own API
  cannot create (a library nobody can read, a content-generation bump). It
  never answers a question about access.
* ``gateway`` — ``kb_app``, RLS applies. Every assertion about what a caller
  may do or see goes through this one.
* ``provisioner`` — ``kb_provisioner``, the one-shot. NOSUPERUSER,
  NOBYPASSRLS, five tables of grants.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import sys
import uuid
from uuid import UUID

# --------------------------------------------------------------------------
# Path setup, before anything in this process imports `kb`.
#
# The venv has an editable install pointing at the MAIN checkout, and
# `tests/conftest.py` above us is happy with that. In a worktree it is wrong:
# `kb` would resolve to somebody else's source tree and this suite would
# quietly test a package that is not the one under review. Inserting the
# worktree's `src` first makes the worktree win, and the second entry makes
# `doubles.py` importable as a plain module — it is documentation of what is
# NOT real here, and documentation buried in a conftest is documentation
# nobody reads.
# --------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "src"), _HERE):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# NOTE. This makes the worktree authoritative for THIS suite, and it is enough
# when the suite is run on its own. It cannot undo a `kb` that a sibling suite
# imported first through the shared venv's editable install, which points at
# the main checkout — purging sys.modules to fix that was tried and it breaks
# C09's catalogue suite, so the honest answer is: run the gates. `just check`
# and `just integration` export PYTHONPATH=src (C01's justfile), and both
# exit 0. `pytest` with no PYTHONPATH inside a worktree is not a supported
# invocation and is recorded as such in docs/handoff/results/C15.json.
# --------------------------------------------------------------------------
# isort: off
import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import make_conninfo  # noqa: E402

from kb.access.policy import Principal, transaction_identity  # noqa: E402

# isort: on

ROOT = _ROOT
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
C15_DB = "kb_c15"

#: A LOGIN role that holds kb_provisioner, so the one-shot code path is
#: exercised as a real non-superuser connection rather than by a superuser
#: with a SET ROLE. A superuser would bypass RLS and every access assertion
#: below would pass vacuously — which is the mistake C09's own conftest
#: documents.
PROVISIONER_LOGIN = "kb_provisioner_login"


# --------------------------------------------------------------- connections


class SqlRunner:
    """Run one statement as a chosen role and principal. Returns (rc, output).

    Every call opens a fresh connection, so there is no session state left
    over from a previous test and nothing to forget to clear. A denial comes
    back as rc=1 with the server's own message and SQLSTATE, so an assertion
    can be on the reason rather than on incidental formatting.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def __call__(
        self,
        statement: str,
        *,
        role: str | None = None,
        principal: UUID | None = None,
        params: tuple | None = None,
    ) -> tuple[int, str]:
        try:
            with psycopg.connect(self._dsn, autocommit=True) as conn:
                if role is not None:
                    conn.execute("SET ROLE " + _ident(role))
                if principal is not None:
                    conn.execute("SELECT set_config('app.principal', %s, false)", (str(principal),))
                cur = conn.execute(statement, params)
                return 0, _render(cur)
        except psycopg.Error as exc:
            return 1, f"[{exc.sqlstate}] {exc}"


def _ident(name: str) -> str:
    """Quote an identifier. Inputs are module constants, never request data."""
    return sql.Identifier(name).as_string(None)  # type: ignore[arg-type]


def _render(cur: psycopg.Cursor) -> str:
    if cur.description is None:
        return ""
    return "\n".join(
        "\t".join("" if value is None else str(value) for value in row) for row in cur.fetchall()
    )


@pytest.fixture(scope="session")
def c15_dsn(pg_server) -> str:
    """A dedicated catalogue on the shared real server."""
    base = pg_server.get_uri()
    with psycopg.connect(base, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C15_DB,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(C15_DB)))
    return make_conninfo(base, dbname=C15_DB)


@pytest.fixture(scope="session")
def schema(c15_dsn: str) -> bool:
    """Apply 0001 → 0004 ONCE per session. Structure only, no content.

    0003_catalog_bindings.sql and 0003_identity_sessions.sql sort before
    0004_provisioning.sql, which is the order they are created in and the
    order they depend on each other in.
    """
    script = "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS)
    with psycopg.connect(c15_dsn, autocommit=True) as conn:
        try:
            conn.execute(script)
        except psycopg.Error as exc:
            raise RuntimeError(f"C15 migration failed:\n{exc}") from exc
    return True


@pytest.fixture(scope="session")
def run_sql(schema, c15_dsn: str) -> SqlRunner:
    return SqlRunner(c15_dsn)


@pytest.fixture(scope="session")
def provisioner_dsn(schema, c15_dsn: str) -> str:
    """A DSN for a real, non-superuser connection that holds kb_provisioner.

    Created if absent. The role exists so the CLI's "refuse to run as
    postgres" branch and its ordinary branch are both exercisable against the
    real server rather than asserted in prose.
    """
    with psycopg.connect(c15_dsn, autocommit=True) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (PROVISIONER_LOGIN,)
            ).fetchone()
            is None
        ):
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS").format(
                    sql.Identifier(PROVISIONER_LOGIN)
                )
            )
            conn.execute(
                sql.SQL("GRANT kb_provisioner TO {}").format(sql.Identifier(PROVISIONER_LOGIN))
            )
    return make_conninfo(c15_dsn, user=PROVISIONER_LOGIN)


@pytest.fixture(scope="session")
def admin(schema, c15_dsn: str):
    """The migration role, for arranging fixtures. Never for access answers."""
    with psycopg.connect(c15_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def provisioner(provisioner_dsn: str):
    """A live connection as the one-shot role, for the orchestration tests."""
    with psycopg.connect(provisioner_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def gateway(c15_dsn: str):
    """A live connection as kb_app — the role RLS is written for.

    ``options=-c role=kb_app`` rather than a superuser connection: a
    superuser BYPASSES RLS, which would make every negative assertion in this
    directory pass for the wrong reason.
    """
    conn = psycopg.connect(c15_dsn, autocommit=True, options="-c role=kb_app")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session")
def gateway_dsn(c15_dsn: str) -> str:
    """A DSN for a kb_app connection, for code that opens its own connection."""
    return make_conninfo(c15_dsn, options="-c role=kb_app")


@pytest.fixture
def acting(gateway):
    """Run a raw statement as one named person of the world.

    The product code sets the transaction-local identity itself; a test that
    pokes the database directly has to do it explicitly, or it silently reads
    the default-deny view (no rows) and concludes something false. Getting
    this wrong is how a test suite ends up asserting on an empty result.
    """

    @contextlib.contextmanager
    def _act(world: World, who: str = "reader"):
        people = {
            "owner": world.manager,
            "colleague": world.reader,
            "stranger": world.nobody,
        }
        with transaction_identity(gateway, people[who]):
            yield gateway

    return _act


# ------------------------------------------------------------------- the world


class World:
    """Arranges a scenario as the migration role.

    Seeding as the owner is the only way to create the rows the negative tests
    need — a library nobody can read, a bumped content generation. Every
    assertion about what a caller may see still goes through ``app.principal``
    as ``kb_app``.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self.account_id = uuid.uuid4()
        self.owner = uuid.uuid4()
        self.colleague = uuid.uuid4()
        self.stranger = uuid.uuid4()
        self._org: UUID | None = None

    def principal(self, who: UUID) -> Principal:
        return Principal(principal_id=who, account_id=self.account_id, generation_watermark=1)

    @property
    def manager(self) -> Principal:
        return self.principal(self.owner)

    @property
    def reader(self) -> Principal:
        return self.principal(self.colleague)

    @property
    def nobody(self) -> Principal:
        return self.principal(self.stranger)

    def organisation(self) -> UUID:
        if self._org is None:
            self._org = uuid.uuid4()
            self.conn.execute(
                "INSERT INTO kb.organisation (id, name) VALUES (%s, %s)",
                (self._org, f"c15-org-{self._org.hex[:8]}"),
            )
        return self._org

    def library(
        self, *, name: str | None = None, kind: str = "reference", audience: str = "private"
    ) -> UUID:
        lib = uuid.uuid4()
        self.conn.execute(
            "INSERT INTO kb.library (id, organisation_id, name, kind, audience_scope) "
            "VALUES (%s, %s, %s, %s, %s)",
            (lib, self.organisation(), name or f"c15-lib-{lib.hex[:8]}", kind, audience),
        )
        return lib

    def grant(self, library_id: UUID, principal_id: UUID, role: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.library_grant (library_id, principal_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (library_id, principal_id) DO UPDATE SET role = EXCLUDED.role",
            (library_id, principal_id, role),
        )

    def library_with(
        self, *roles: tuple[str, str], name: str | None = None
    ) -> tuple[UUID, dict[str, UUID]]:
        """A library plus named grants: ``world.library_with(("owner", "manager"))``."""
        library = self.library(name=name)
        people = {"owner": self.owner, "colleague": self.colleague, "stranger": self.stranger}
        for who, role in roles:
            self.grant(library, people[who], role)
        return library, people

    def generation_policy(
        self,
        library_id: UUID,
        *,
        allowed: bool = True,
        requires_owner_start: bool = True,
        max_concurrency: int = 1,
    ) -> None:
        self.conn.execute(
            "INSERT INTO kb.generation_policy "
            "(library_id, generation_allowed, max_concurrency, requires_owner_start) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (library_id) DO UPDATE SET "
            "generation_allowed = EXCLUDED.generation_allowed, "
            "max_concurrency = EXCLUDED.max_concurrency, "
            "requires_owner_start = EXCLUDED.requires_owner_start",
            (library_id, allowed, max_concurrency, requires_owner_start),
        )

    def bump_content_generation(self, library_id: UUID, generation: int) -> None:
        """Move the library's content counter.

        No runtime role may do this: ``kb.library`` has a SELECT and an INSERT
        policy and no UPDATE policy, so no manager, curator or gateway can bump
        it. Reported as a finding in docs/handoff/results/C15.json — the card
        that owns content mutation has to add the path, and until it does this
        is done by the owner, which is what a test world is for.
        """
        self.conn.execute(
            "UPDATE kb.library SET generation = %s WHERE id = %s", (generation, library_id)
        )

    def account_row(self, library_id: UUID) -> dict[str, object] | None:
        row = self.conn.execute(
            "SELECT library_id, account_ref, read_identity, index_identity, root_path, "
            "dimension, embedding_profile, acl, state FROM kb.index_account WHERE library_id = %s",
            (library_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "library_id": row[0],
            "account_ref": row[1],
            "read_identity": row[2],
            "index_identity": row[3],
            "root_path": row[4],
            "dimension": row[5],
            "embedding_profile": row[6],
            "acl": row[7],
            "state": row[8],
        }

    def insert_account(
        self,
        library_id: UUID,
        *,
        account_ref: str | None = None,
        read_identity: str | None = None,
        index_identity: str | None = None,
        root_path: str = "/index",
        dimension: int = 1024,
        embedding_profile: str = "qwen3-embedding-0.6b",
        acl: dict[str, object] | None = None,
        state: str = "pending",
    ) -> None:
        """An account row, bypassing the one-shot. For arranging, not for use."""
        hexlib = f"{library_id.hex}"
        document = acl or {
            "inherit_from_parent": False,
            "entries": [
                {"principal": f"kb-svc-read-{hexlib}", "rights": ["read"]},
                {"principal": f"kb-svc-index-{hexlib}", "rights": ["index", "read"]},
            ],
        }
        self.conn.execute(
            "INSERT INTO kb.index_account (library_id, account_ref, read_identity, "
            "index_identity, root_path, dimension, embedding_profile, acl, state) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                library_id,
                account_ref or f"kb-lib-{hexlib}",
                read_identity or f"kb-svc-read-{hexlib}",
                index_identity or f"kb-svc-index-{hexlib}",
                root_path,
                dimension,
                embedding_profile,
                json.dumps(document),
                state,
            ),
        )

    def credential_ref(self, library_id: UUID, identity: str, secret_ref: str) -> None:
        self.conn.execute(
            "INSERT INTO kb.index_credential_ref (library_id, identity, secret_ref) "
            "VALUES (%s, %s, %s)",
            (library_id, identity, secret_ref),
        )

    def published_generation(self, library_id: UUID, generation: int) -> None:
        """A finished, current index generation for a library."""
        self.conn.execute(
            "INSERT INTO kb.index_generation (library_id, generation, state, canary_uri, "
            "started_at, finished_at, content_hash, started_by) "
            "VALUES (%s, %s, 'current', NULL, now(), now(), %s, %s)",
            (library_id, generation, f"{generation:064x}", self.owner),
        )

    def building_generation(self, library_id: UUID, generation: int) -> None:
        """A generation that was opened and never finished. The stuck case."""
        self.conn.execute(
            "INSERT INTO kb.index_generation (library_id, generation, state, content_hash, "
            "started_by) VALUES (%s, %s, 'building', %s, %s)",
            (library_id, generation, f"{generation:064x}", self.owner),
        )

    def knowledge(
        self, library_id: UUID, *, title: str = "canary", publication: str = "published"
    ) -> UUID:
        row = self.conn.execute(
            "INSERT INTO kb.knowledge (library_id, statement, kind, publication) "
            "VALUES (%s, %s, 'assertion', %s) RETURNING id",
            (library_id, f"{title} for {library_id.hex[:8]}", publication),
        ).fetchone()
        assert row is not None
        return row[0]


@pytest.fixture
def world(admin) -> World:
    return World(admin)


@pytest.fixture
def ready_library(world) -> UUID:
    """A library with an account, an opt-in and a current index generation.

    The ordinary steady state: what a search is expected to work against. Each
    negative test starts from here and breaks exactly one thing.
    """
    library, _people = world.library_with(("owner", "manager"), ("colleague", "reader"))
    world.insert_account(library, state="ready")
    world.generation_policy(library)
    world.published_generation(library, 1)
    return library


@pytest.fixture(autouse=True)
def _close_open_generations(admin):
    """No test may leak the installation's single generation slot.

    The one-slot index is installation-wide — that is the product decision,
    and several tests exist to prove it — so an unfinished generation left
    behind by one test blocks every test that follows. This is environment
    cleanup, in the same category as clearing a temporary directory: inside a
    test the constraint is completely real and asserted, and between tests the
    world is put back.
    """
    yield
    open_rows = admin.execute(
        "SELECT library_id, generation FROM kb.index_generation WHERE state = 'building'"
    ).fetchall()
    for library_id, generation in open_rows:
        admin.execute(
            "UPDATE kb.index_generation SET state = 'retired', finished_at = now(), "
            "failure_reason = 'torn down between tests' "
            "WHERE library_id = %s AND generation = %s AND state = 'building'",
            (library_id, generation),
        )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: requires a real PostgreSQL server")
    config.addinivalue_line(
        "markers",
        "deliberately_generative: the one test that goes through the generative door on "
        "purpose, as the control for the zero-call assertion",
    )
    config.addinivalue_line(
        "markers", "requires_openviking: needs a live OpenViking server (absent in C15)"
    )
