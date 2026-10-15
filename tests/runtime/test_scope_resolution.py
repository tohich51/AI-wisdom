"""Scope resolution for a CONCRETE request path.

Sol's P1 on 28003628: `operation_for` looked the path up as an exact route
template while `runtime_identity` hands it `request.url.path`, which carries
real UUIDs. Every parameterized operation therefore resolved to nothing and
was closed — `GET /api/libraries/<uuid>/sources` and
`POST /api/v1/uses/<uuid>/applied` answered 403 to a caller holding exactly the
scope the operation declares. 122 of 166 operation/prefix combinations were
closed.

The previous registry tests passed because they looked up the template itself,
which is the one input the exact pass answers. So these tests deliberately do
not use a literal `{id}` anywhere: every path below is what a client actually
sends, and the two end-to-end cases drive the real `kb.runtime_identity`
bearer branch rather than the scope helper.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from kb.access.bearer_policy import http_scope, scope_for_request
from kb.access.identity import VerifiedIdentity
from kb.contracts.operations import (
    ALIAS_PREFIX,
    API_PREFIXES,
    CANONICAL_PREFIX,
    OPERATIONS,
    canonical_api_path,
    operation_for,
    operation_for_template,
)
from kb.runtime_identity import create_resolver

#: A real, concrete id. The point of the regression is that it is not "{id}".
UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"
UUID_C = "33333333-3333-3333-3333-333333333333"

ALL_SCOPES = frozenset({"kb:read", "kb:contribute", "kb:experience", "kb:export", "kb:admin"})


def concrete(path: str) -> str:
    """The URL a client would actually send for a declared route template."""
    segments = []
    for segment in path.split("/"):
        if not segment:
            continue
        segments.append(UUID_A if segment.startswith("{") else segment)
    return "/" + "/".join(segments)


# ------------------------------------------------- every operation, real paths


@pytest.mark.parametrize("prefix", API_PREFIXES)
@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda op: f"{op.method}-{op.name}")
def test_every_operation_resolves_at_a_concrete_path_under_both_prefixes(operation, prefix):
    """The whole point: a real UUID resolves to its registered operation."""
    path = f"{prefix}{concrete(operation.path)}"
    assert operation_for(operation.method, path) is operation
    assert operation_for_template(operation.method, operation.path) is operation


@pytest.mark.parametrize("prefix", API_PREFIXES)
@pytest.mark.parametrize("operation", OPERATIONS, ids=lambda op: f"{op.method}-{op.name}")
def test_concrete_scope_equals_the_frozen_table(operation, prefix):
    """Resolving at a concrete path must not change the answer, only find it.

    The 83 registered scopes are inherited policy (F2-M6, routed to the
    coordinator); the point here is that reaching them by UUID returns the same
    scope the frozen pattern table always gave, not a different one.
    """
    path = f"{prefix}{concrete(operation.path)}"
    assert scope_for_request(operation.method, path) == http_scope(operation.method, path)


def test_the_two_paths_sol_named_resolve():
    assert scope_for_request("GET", f"/api/libraries/{UUID_A}/sources") == "kb:read"
    assert scope_for_request("POST", f"{ALIAS_PREFIX}/uses/{UUID_A}/applied") == "kb:experience"
    assert scope_for_request("POST", f"{CANONICAL_PREFIX}/uses/{UUID_A}/applied") == "kb:experience"


def test_template_lookup_agrees_with_the_exact_lookup():
    """A template must still resolve to itself, so the existing registry checks
    and the route-table comparison keep working unchanged."""
    disagreements = [
        f"{op.method} {op.path}"
        for op in OPERATIONS
        if operation_for_template(op.method, op.path) is not op
    ]
    assert disagreements == []


def test_a_parameter_does_not_span_a_slash():
    """/libraries/{library_id} must not swallow /libraries/a/b."""
    assert operation_for("GET", f"/{CANONICAL_PREFIX[5:]}") is None  # sanity: nonsense path
    assert operation_for("GET", f"/api/libraries/{UUID_A}/not-a-route") is None
    assert operation_for("GET", f"/api/libraries/{UUID_A}/{UUID_B}/deeper") is None


# ------------------------------------------------------- static over parameter


def test_a_static_segment_outranks_a_parameter():
    """The one ambiguous pair in the registry, resolved the way the router does.

    `GET /research/sources` and `GET /research/{run_id}` both match the
    concrete path /research/sources. Starlette declares the literal route first,
    so the static operation is the one actually served, and the registry has to
    agree or a request to the static route would be scoped as the other one.
    """
    from kb.contracts.operations import OPERATION_ROUTES

    static = OPERATION_ROUTES[("GET", "/research/sources")]
    parameter = OPERATION_ROUTES[("GET", "/research/{run_id}")]

    assert scope_for_request("GET", "/api/v1/research/sources") == static.scope
    assert operation_for("GET", "/api/v1/research/sources") is static
    assert operation_for("GET", f"/api/v1/research/{UUID_A}") is parameter
    # The two are genuinely different operations, so the choice is load-bearing.
    assert static is not parameter


def test_an_unknown_protected_path_is_still_closed():
    assert scope_for_request("GET", f"/api/v1/libraries/{UUID_A}/nope") is None
    assert operation_for("GET", f"/api/v1/libraries/{UUID_A}/nope") is None


def test_a_trailing_slash_is_still_closed():
    """The router redirects /libraries/ to /libraries, so no operation is
    serving the slashed form and no scope is granted for it."""
    assert operation_for("GET", f"/api/v1/libraries/{UUID_A}/sources/") is None


def test_a_mismatched_method_is_still_closed():
    """Scope is per (method, path). A registered GET template does not answer a
    POST on the same path."""
    assert scope_for_request("GET", f"/api/v1/libraries/{UUID_A}/sources") == "kb:read"
    assert scope_for_request("POST", f"/api/v1/libraries/{UUID_A}/sources") is None
    assert scope_for_request("DELETE", f"/api/v1/libraries/{UUID_A}/sources") is None


def test_paths_outside_the_api_prefixes_are_untouched():
    """/app, /auth, /static and /mcp are not governed by the operations table."""
    for path in ("/app/experience", "/auth/session", "/static/app.js", "/healthz"):
        assert scope_for_request("GET", path) == http_scope("GET", path)
    assert canonical_api_path("/app/experience") is None


# -------------------------------------- the real bearer boundary, end to end


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return (1,)  # the membership row exists; admission passes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = FakeCursor()

    def cursor(self, *args, **kwargs):
        return self.cursor_instance

    def execute(self, *args, **kwargs):
        self.cursor_instance.execute(*args[:2])


class RecordingFactory:
    def __init__(self) -> None:
        self.leases = 0

    @contextmanager
    def __call__(self):
        self.leases += 1
        yield FakeConnection()


class StubVerifier:
    """Presents a token that verified, carrying exactly the scopes under test."""

    def __init__(self, scopes: frozenset[str]) -> None:
        self._scopes = scopes

    def verify(self, token: str) -> VerifiedIdentity:
        now = dt.datetime.now(dt.UTC)
        return VerifiedIdentity(
            issuer="https://id.example/realms/kb",
            subject="reviewer",
            principal_id=UUID(UUID_A),
            account_id=UUID(UUID_C),
            scopes=self._scopes,
            issued_at=now,
            expires_at=now + dt.timedelta(hours=1),
            client_id="reviewer-client",
        )


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "server": ("gateway.example", 443),
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer a-real-token")],
            "state": {},
        }
    )


def _resolver(scopes: frozenset[str], monkeypatch):
    """The real resolver from kb.runtime_identity, with only the pool stubbed.

    Everything under test is the real code: the bearer branch of `resolve`, the
    membership admission, `bearer_principal` and the scope lookup. Only the
    database lease and the identity GUCs are replaced, because the question
    here is which scope the operation resolves to, not what the GUCs say.
    """
    monkeypatch.setattr("kb.runtime_identity.transaction_identity", lambda conn, who: _null())
    return create_resolver(RecordingFactory(), object(), object(), StubVerifier(scopes))[0]


def _null():
    from contextlib import nullcontext

    return nullcontext()


def test_a_get_with_a_concrete_uuid_passes_the_real_bearer_boundary(monkeypatch):
    """The GET Sol named. Before the fix this raised AccessDenied with a caller
    holding every scope, because the lookup was handed a UUID and found no
    template."""
    resolve = _resolver(ALL_SCOPES, monkeypatch)
    request = _request("GET", f"/api/libraries/{UUID_A}/sources")
    principal = resolve(request)
    assert principal.principal_id == UUID(UUID_A)
    assert request.state._kb_identity_resolved is True


def test_a_post_with_a_concrete_uuid_passes_the_real_bearer_boundary(monkeypatch):
    """The POST Sol named, on the canonical prefix."""
    resolve = _resolver(ALL_SCOPES, monkeypatch)
    request = _request("POST", f"/api/v1/uses/{UUID_A}/applied")
    resolve(request)
    assert request.state._kb_identity_resolved is True


def test_the_same_post_on_the_alias_prefix_also_passes(monkeypatch):
    """The alias must reach the same operation, not a different answer."""
    resolve = _resolver(ALL_SCOPES, monkeypatch)
    request = _request("POST", f"/api/uses/{UUID_A}/applied")
    resolve(request)
    assert request.state._kb_identity_resolved is True


def _assert_denied(resolve, method: str, path: str) -> None:
    """The real boundary answers 403, not an internal exception type.

    ``resolve`` catches the scope denial and re-raises it as an HTTPException,
    so the observable contract is the status code. Asserting on ``AccessDenied``
    instead would pin an implementation detail the caller never sees.
    """
    with pytest.raises(HTTPException) as denied:
        resolve(_request(method, path))
    assert denied.value.status_code == 403


def test_an_unknown_path_is_denied_at_the_real_boundary(monkeypatch):
    resolve = _resolver(ALL_SCOPES, monkeypatch)
    _assert_denied(resolve, "GET", f"/api/v1/libraries/{UUID_A}/not-a-route")


def test_a_mismatched_method_is_denied_at_the_real_boundary(monkeypatch):
    """A registered GET path, called as a POST, is closed even with kb:admin."""
    resolve = _resolver(ALL_SCOPES, monkeypatch)
    _assert_denied(resolve, "POST", f"/api/v1/libraries/{UUID_A}/sources")


def test_a_missing_scope_is_still_denied_at_the_real_boundary(monkeypatch):
    """Concrete resolution grants the operation's scope, not a blanket pass."""
    resolve = _resolver(frozenset({"kb:read"}), monkeypatch)
    _assert_denied(resolve, "POST", f"/api/v1/uses/{UUID_A}/applied")
