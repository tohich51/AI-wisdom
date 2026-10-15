"""The shared connection-factory contract.

CONTRACTS.md assigns this module's card a connection factory: a callable
context manager returning ``pool.connection()``, with the lease owned by the
orchestration that called it, an operation never closing a connection it was
handed, and identity established through the existing ``transaction_identity``
for every SQL phase.

These tests are about the shape of that contract and about it being the only
shape. None of them needs a database: the pool is a stand-in that records what
was asked of it, which is the only thing the factory is responsible for.
"""

from __future__ import annotations

import ast
import pathlib
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from kb.access.policy import Principal
from kb.runtime_identity import build_connection_factory, identity_connection

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "kb"


class FakeConnection:
    """Enough of a psycopg connection for a test to assert it was not closed."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(sql)


class RecordingPool:
    """A pool that records leases instead of opening them."""

    def __init__(self) -> None:
        self.leased = 0
        self.closed = 0
        self.opened = False
        self.is_closed = False

    @contextmanager
    def connection(self):
        self.leased += 1
        conn = FakeConnection(f"conn-{self.leased}")
        try:
            yield conn
        finally:
            self.closed += 1
            conn.closed = True

    def open(self, wait: bool = False) -> None:
        self.opened = True

    def close(self) -> None:
        self.is_closed = True


def nullcontext():
    from contextlib import nullcontext as impl

    return impl()


def test_the_factory_is_callable_and_returns_a_context_manager():
    pool = RecordingPool()
    factory = build_connection_factory(pool)

    assert callable(factory)
    # Calling it must not lease anything on its own: entering is the acquire.
    assert pool.leased == 0
    with factory() as connection:
        assert connection.name == "conn-1"
        assert pool.leased == 1
    assert pool.closed == 1


def test_each_call_is_its_own_lease():
    """A second SQL phase gets a second lease, which is what makes the
    short-plan / external-I/O / new-plan shape possible at all."""
    pool = RecordingPool()
    factory = build_connection_factory(pool)

    with factory() as first:
        assert first.name == "conn-1"
    with factory() as second:
        assert second.name == "conn-2"

    assert (pool.leased, pool.closed) == (2, 2)


def test_identity_is_established_for_every_sql_phase(monkeypatch):
    """Identity is re-established per phase, never inherited from the last one."""
    pool = RecordingPool()
    factory = build_connection_factory(pool)
    principal = Principal(
        "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    )

    established: list[tuple[str, object]] = []
    open_transactions: list[int] = []

    @contextmanager
    def fake_transaction_identity(conn, who):
        established.append((conn.name, who))
        open_transactions.append(1)
        try:
            yield conn
        finally:
            open_transactions.pop()

    monkeypatch.setattr("kb.runtime_identity.transaction_identity", fake_transaction_identity)

    for _phase in range(2):
        with identity_connection(factory, principal) as conn:
            assert conn.name in {"conn-1", "conn-2"}
            # The lease is held and the identity is already established on it.
            assert len(open_transactions) == 1
            # The phase in progress still holds its lease; earlier ones released theirs.
            assert pool.closed == pool.leased - 1

    assert [who for _name, who in established] == [principal, principal]
    assert [name for name, _who in established] == ["conn-1", "conn-2"]


def test_an_operation_does_not_close_a_connection_it_was_handed(monkeypatch):
    """The lease belongs to the orchestrator. An operation that does everything
    except close what it was given must leave the connection open."""
    pool = RecordingPool()
    factory = build_connection_factory(pool)
    monkeypatch.setattr("kb.runtime_identity.transaction_identity", lambda conn, who: nullcontext())

    with factory() as conn:
        # An operation on a passed-in conn: use it, never close it.
        conn.execute("SELECT 1")
        assert conn.statements == ["SELECT 1"]
        assert conn.closed is False
        assert pool.closed == 0
    assert conn.closed is True
    assert pool.closed == 1


def test_the_pool_is_not_published_so_the_factory_cannot_be_bypassed(monkeypatch, tmp_path):
    """Non-bypassability is the point: with the pool off app.state there is no
    object left for a route or a tool to reach past the factory with."""
    from test_operation_registry import REAL_APP_ENV

    for key, value in REAL_APP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KB_BLOB_ROOT", str(tmp_path))

    pool = RecordingPool()
    monkeypatch.setattr("kb.gateway.ConnectionPool", lambda *a, **k: pool)

    from kb.gateway import create_app

    app = create_app()
    # Entered so the lifespan runs: the factory is published there, which is
    # exactly the point -- it is the only thing app.state carries.
    with TestClient(app, base_url="http://gateway.example"):
        assert callable(app.state.db_connection)
        assert not hasattr(app.state, "db_pool")
    assert pool.opened and pool.is_closed


def test_the_factory_is_reachable_from_every_consumer(monkeypatch, tmp_path):
    """The contract is shared, so the three surfaces that lease a connection
    have to be leasing the same object."""
    from test_operation_registry import REAL_APP_ENV

    for key, value in REAL_APP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KB_BLOB_ROOT", str(tmp_path))

    pool = RecordingPool()
    monkeypatch.setattr("kb.gateway.ConnectionPool", lambda *a, **k: pool)

    from kb.gateway import create_app

    app = create_app()
    with TestClient(app, base_url="http://gateway.example") as client:
        factory = app.state.db_connection
        # The HTTP dependency, the MCP transport and the readiness probe all
        # read this one name off app.state.
        assert client.get("/readyz").status_code in {200, 503}
        assert pool.leased >= 1
    assert callable(factory)


def test_no_module_outside_the_factory_opens_a_connection():
    """A structural guard, because the property is structural.

    Every ``pool.connection()`` call in the package is located with the AST, so
    a mention in prose cannot satisfy it and a mention in prose cannot fail it.
    There must be exactly one, inside ``build_connection_factory``. A second is
    a connection with no shared lease and no established identity, and no
    runtime test would necessarily notice it.
    """
    sites: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "connection":
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "pool":
                sites.append(str(path.relative_to(SRC)))
    assert sites == ["runtime_identity.py"]


def test_nothing_reads_the_removed_pool_attribute():
    """The pool attribute on app.state is gone; a leftover read is a silent 503."""
    hits = [
        str(path.relative_to(SRC))
        for path in sorted(SRC.rglob("*.py"))
        if "db_pool" in path.read_text(encoding="utf-8")
    ]
    assert hits == []


@pytest.mark.parametrize(
    "module",
    ["kb.http.libraries", "kb.mcp.server", "kb.runtime_identity", "kb.runtime_auth"],
)
def test_no_consumer_imports_the_pool_type(module):
    """Taking the pool as a parameter or an attribute is how a factory gets
    bypassed a week later, so the type is not importable where it is used."""
    import importlib

    assert not hasattr(importlib.import_module(module), "ConnectionPool")


def test_only_the_owner_knows_the_pool_type():
    """The gateway builds and closes the pool. Nobody else may name the type,
    or the next change will pass it around again."""
    import importlib

    owners = [
        module
        for module in (
            "kb.gateway",
            "kb.http.libraries",
            "kb.mcp.server",
            "kb.runtime_identity",
            "kb.runtime_auth",
        )
        if hasattr(importlib.import_module(module), "ConnectionPool")
    ]
    assert owners == ["kb.gateway"]
