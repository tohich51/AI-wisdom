"""Focused checks for the SDK backend candidate.

Offline only: no network, no real OAuth, no book content.  Every check
targets one invariant that the acceptance contract names, and each failing
check prints what it expected and what it got.

Run from the portable package with Python 3.12:

    .venv/bin/python tests/test_sdk_backend.py
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import sdk_backend
from sdk_backend import (
    DEFAULT_SCOPE,
    ProtectedTokenStorage,
    SdkBackend,
    SdkOAuthProvider,
    SdkTransportError,
    _classify,
)
from mcp.client.auth.oauth2 import OAuthFlowError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.exceptions import MCPError

# Synthetic fixture: production constructor still requires an explicit issuer.
class SdkBackend(sdk_backend.SdkBackend):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("issuer", "https://id.example/realms/knowledge-hub")
        super().__init__(*args, **kwargs)

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(name)
        print(f"PASS {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"FAIL {name} :: {detail}")


def check_raises(name: str, exc_type, fn, detail: str = "") -> None:
    try:
        fn()
    except exc_type as exc:
        PASSES.append(name)
        print(f"PASS {name} :: {type(exc).__name__}")
        return
    except Exception as exc:  # noqa: BLE001 - the check reports the wrong type
        FAILURES.append(f"{name}: raised {type(exc).__name__} instead of {exc_type.__name__}")
        print(f"FAIL {name} :: raised {type(exc).__name__}: {exc}")
        return
    FAILURES.append(f"{name}: nothing raised, expected {exc_type.__name__}")
    print(f"FAIL {name} :: nothing raised")


# --------------------------------------------------------------------------- #
# 1. the grant belongs to this client and is stored protected
# --------------------------------------------------------------------------- #


def test_storage_is_protected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "oauth"
        storage = ProtectedTokenStorage(
            directory, "kb-mcp", "http://127.0.0.1:19289/callback/sdk-backend",
            DEFAULT_SCOPE, "https://id.example/realms/knowledge-hub",
        )
        dir_mode = stat.S_IMODE(os.stat(directory).st_mode)
        check("storage directory is 0700", dir_mode == 0o700, f"got {oct(dir_mode)}")

        token = OAuthToken(
            access_token="opaque-not-a-real-token",
            token_type="Bearer",
            expires_in=300,
            scope=DEFAULT_SCOPE,
            refresh_token="opaque-refresh",
        )
        asyncio.run(storage.set_tokens(token))
        file_mode = stat.S_IMODE(os.stat(storage.path).st_mode)
        check("storage file is 0600", file_mode == 0o600, f"got {oct(file_mode)}")
        asyncio.run(
            storage.set_client_info(
                OAuthClientInformationFull(
                    client_id="kb-mcp",
                    redirect_uris=["http://127.0.0.1:19289/callback/sdk-backend"],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=DEFAULT_SCOPE,
                )
            )
        )
        reloaded = ProtectedTokenStorage(
            directory, "kb-mcp", "http://127.0.0.1:19289/callback/sdk-backend",
            DEFAULT_SCOPE, "https://id.example/realms/knowledge-hub",
        )
        check("token survives a restart", reloaded._tokens is not None, "token lost")
        check(
            "expiry survives a restart",
            reloaded._expires_at is not None and reloaded._expires_at > time.time(),
            f"expires_at={reloaded._expires_at}",
        )
        check(
            "client_info survives a restart",
            reloaded.get_client_info_sync() is not None
            if hasattr(reloaded, "get_client_info_sync")
            else reloaded._client_info is not None,
            "client_info lost",
        )
        # The stored file must not contain the plaintext token in a place that
        # a casual reader of the directory would find; it is 0600 by design.
        raw = json.loads(storage.path.read_text())
        check("stored state names only this client", raw.get("client_id") == "kb-mcp", str(raw.get("client_id")))


# --------------------------------------------------------------------------- #
# 2. the provider restores the expiry, so a stale token is not treated as live
# --------------------------------------------------------------------------- #


class _FakeStorage(ProtectedTokenStorage):
    """Storage with a fixed issuer, a seeded registration and no discovery."""

    def __init__(self, directory: str, expires_at: float | None, tokens: OAuthToken | None):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.directory / "oauth-state.json"
        self.client_id = "kb-mcp"
        self.redirect_uri = "http://127.0.0.1:19289/callback/sdk-backend"
        self.scope = DEFAULT_SCOPE
        self.issuer = "https://id.example/realms/knowledge-hub"
        self._lock = __import__("threading").Lock()
        self._tokens = tokens
        self._expires_at = expires_at
        self._metadata = None
        # The registration is seeded exactly as the backend seeds it.
        self._client_info = OAuthClientInformationFull(
            client_id="kb-mcp",
            redirect_uris=["http://127.0.0.1:19289/callback/sdk-backend"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=DEFAULT_SCOPE,
            client_name="ai-wisdom-sdk-backend",
            issuer=self.issuer,
        )


def test_expiry_is_restored() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expired = OAuthToken(
                access_token="opaque", token_type="Bearer", expires_in=1,
                scope=DEFAULT_SCOPE, refresh_token="opaque-refresh",
            )
            storage = _FakeStorage(tmp, time.time() - 60, expired)
            provider = SdkOAuthProvider(
                server_url="https://ai-wisdom.example/mcp",
                client_metadata=sdk_backend.OAuthClientMetadata(
                    client_name="t",
                    redirect_uris=["http://127.0.0.1:19289/callback/sdk-backend"],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=DEFAULT_SCOPE,
                ),
                storage=storage,
            )
            # Do not let the provider reach the network: the stored metadata
            # is fresh, so no discovery is needed.
            storage._metadata = {
                "fetched_at": time.time(),
                "auth_server_url": "https://id.example/realms/knowledge-hub",
                "protected_resource": {
                    "resource": "https://ai-wisdom.example/mcp",
                    "authorization_servers": ["https://id.example/realms/knowledge-hub"],
                },
                "authorization_server": {
                    "issuer": "https://id.example/realms/knowledge-hub",
                    "authorization_endpoint": "https://id.example/realms/knowledge-hub/auth",
                    "token_endpoint": "https://id.example/realms/knowledge-hub/token",
                },
            }
            await provider._initialize()
            check(
                "expired stored token is not treated as valid",
                not provider.context.is_token_valid(),
                "the SDK would consider the stale token live",
            )
            check(
                "the SDK still has a refresh token to use",
                provider.context.can_refresh_token(),
                "no refresh token available",
            )

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# 3. metadata is validated: a foreign issuer or a foreign token endpoint
#    must fail closed instead of falling back to the resource origin
# --------------------------------------------------------------------------- #


def _provider_with_metadata(tmp: str, metadata: dict[str, Any]) -> SdkOAuthProvider:
    storage = _FakeStorage(tmp, None, None)
    storage._metadata = metadata
    provider = SdkOAuthProvider(
        server_url="https://ai-wisdom.example/mcp",
        client_metadata=sdk_backend.OAuthClientMetadata(
            client_name="t",
            redirect_uris=["http://127.0.0.1:19289/callback/sdk-backend"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=DEFAULT_SCOPE,
        ),
        storage=storage,
    )
    return provider


def test_metadata_validation_fails_closed() -> None:
    good = {
        "fetched_at": time.time(),
        "auth_server_url": "https://id.example/realms/knowledge-hub",
        "protected_resource": {
            "resource": "https://ai-wisdom.example/mcp",
            "authorization_servers": ["https://id.example/realms/knowledge-hub"],
        },
        "authorization_server": {
            "issuer": "https://id.example/realms/knowledge-hub",
            "authorization_endpoint": "https://id.example/realms/knowledge-hub/auth",
            "token_endpoint": "https://id.example/realms/knowledge-hub/token",
        },
    }

    with tempfile.TemporaryDirectory() as tmp:
        provider = _provider_with_metadata(tmp, good)
        asyncio.run(provider._apply_metadata(good))
        check(
            "matching metadata is accepted",
            provider.context.oauth_metadata is not None
            and str(provider.context.oauth_metadata.token_endpoint).endswith("/token"),
            "metadata not installed",
        )

    with tempfile.TemporaryDirectory() as tmp:
        foreign = json.loads(json.dumps(good))
        foreign["authorization_server"]["issuer"] = "https://evil.example/realms/other"
        provider = _provider_with_metadata(tmp, foreign)
        check_raises(
            "metadata with a foreign issuer fails closed",
            OAuthFlowError,
            lambda: asyncio.run(provider._apply_metadata(foreign)),
            "the provider accepted another issuer",
        )

    with tempfile.TemporaryDirectory() as tmp:
        foreign_endpoint = json.loads(json.dumps(good))
        foreign_endpoint["authorization_server"]["token_endpoint"] = "https://evil.example/token"
        provider = _provider_with_metadata(tmp, foreign_endpoint)
        check_raises(
            "metadata with a foreign token endpoint fails closed",
            OAuthFlowError,
            lambda: asyncio.run(provider._apply_metadata(foreign_endpoint)),
            "the provider accepted a foreign token endpoint",
        )

    with tempfile.TemporaryDirectory() as tmp:
        wrong_issuer = json.loads(json.dumps(good))
        wrong_issuer["auth_server_url"] = "https://id.example/realms/other-realm"
        wrong_issuer["authorization_server"]["issuer"] = "https://id.example/realms/other-realm"
        wrong_issuer["authorization_server"]["token_endpoint"] = (
            "https://id.example/realms/other-realm/token"
        )
        provider = _provider_with_metadata(tmp, wrong_issuer)
        check_raises(
            "metadata for another realm fails closed",
            OAuthFlowError,
            lambda: asyncio.run(provider._apply_metadata(wrong_issuer)),
            "the provider accepted another realm",
        )

    with tempfile.TemporaryDirectory() as tmp:
        malformed = json.loads(json.dumps(good))
        malformed["protected_resource"] = "not-an-object"
        provider = _provider_with_metadata(tmp, malformed)
        check_raises(
            "malformed metadata fails closed",
            OAuthFlowError,
            lambda: asyncio.run(provider._apply_metadata(malformed)),
            "the provider accepted malformed metadata",
        )

    with tempfile.TemporaryDirectory() as tmp:
        mismatched_resource = json.loads(json.dumps(good))
        mismatched_resource["protected_resource"]["resource"] = "https://other.example/mcp"
        provider = _provider_with_metadata(tmp, mismatched_resource)
        check_raises(
            "metadata whose resource does not match the server fails closed",
            OAuthFlowError,
            lambda: asyncio.run(provider._apply_metadata(mismatched_resource)),
            "the provider accepted a foreign resource",
        )


# --------------------------------------------------------------------------- #
# 4. the loopback callback listener: exact path, GET only, clean redirect
# --------------------------------------------------------------------------- #


def test_callback_listener() -> None:
    import http.client
    import threading
    import urllib.parse

    with tempfile.TemporaryDirectory() as tmp:
        backend = SdkBackend(
            "https://ai-wisdom.example/mcp",
            tmp,
            callback_port=19391,
            callback_path="/callback/sdk-backend",
        )

        async def run() -> tuple[str, dict[str, str], dict[str, Any]]:
            await backend._ensure_callback_server()
            assert backend._callback_future is not None
            result: dict[str, Any] = {}

            def do_request() -> None:
                conn = http.client.HTTPConnection("127.0.0.1", 19391, timeout=10)
                conn.request("GET", "/callback/sdk-backend?code=abc&state=xyz")
                response = conn.getresponse()
                result["status"] = response.status
                result["location"] = response.getheader("Location") or ""
                response.read()
                conn.close()

            thread = threading.Thread(target=do_request)
            thread.start()
            try:
                query = await asyncio.wait_for(backend._callback_future, timeout=15)
            finally:
                thread.join(timeout=10)
                await backend._close_callback_server()
            return query["code"], query, result

        code, query, result = asyncio.run(run())
        check("callback returns the code", code == "abc", f"got {code!r}")
        check("callback returns the state", query["state"] == "xyz", f"got {query['state']!r}")
        check(
            "the browser is redirected to a URL without the code",
            "code=" not in result.get("location", ""),
            f"location={result.get('location')!r}",
        )

    with tempfile.TemporaryDirectory() as tmp:
        backend = SdkBackend(
            "https://ai-wisdom.example/mcp",
            tmp,
            callback_port=19392,
            callback_path="/callback/sdk-backend",
        )

        async def run() -> int:
            await backend._ensure_callback_server()
            assert backend._callback_future is not None
            loop = asyncio.get_running_loop()
            status: dict[str, int] = {}

            def do_request() -> None:
                conn = http.client.HTTPConnection("127.0.0.1", 19392, timeout=10)
                conn.request("GET", "/wrong/path?code=abc&state=xyz")
                response = conn.getresponse()
                status["code"] = response.status
                response.read()
                conn.close()

            thread = threading.Thread(target=do_request)
            thread.start()
            await asyncio.sleep(0.5)
            thread.join(timeout=10)
            check(
                "a foreign path is not served",
                status.get("code") == 404,
                f"got {status.get('code')}",
            )
            check(
                "a foreign path does not resolve the callback",
                not backend._callback_future.done(),
                "the callback future was resolved by a foreign path",
            )
            backend._callback_future.cancel()
            await backend._close_callback_server()

        asyncio.run(run())


# --------------------------------------------------------------------------- #
# 5. error classification: a typed upstream error keeps the transport alive,
#    an unknown result fails closed
# --------------------------------------------------------------------------- #


def test_error_classification() -> None:
    typed = _classify(MCPError(code=-32602, message="bad params"))
    check(
        "a typed upstream error is reported as a live-server refusal",
        type(typed).__name__ == "NativeRequestError",
        f"got {type(typed).__name__}",
    )
    unknown = _classify(RuntimeError("socket closed"))
    check(
        "an unknown result fails closed",
        type(unknown).__name__ == "SdkTransportError",
        f"got {type(unknown).__name__}",
    )
    timed_out = _classify(TimeoutError("deadline"))
    check(
        "a timeout fails closed",
        type(timed_out).__name__ == "SdkTransportError",
        f"got {type(timed_out).__name__}",
    )


def test_unsupported_result_fails_closed() -> None:
    """An unsupported or malformed result must never become a fake success.

    An empty payload ``{"content": [], "isError": False}`` is indistinguishable
    from a real read for the caller, so the conversion has to fail closed.
    """
    from sdk_backend import _result_to_payload

    check_raises(
        "an unsupported result fails closed",
        SdkTransportError,
        lambda: _result_to_payload(None),
        "the converter fabricated an empty success",
    )
    check_raises(
        "a result without a content list fails closed",
        SdkTransportError,
        lambda: _result_to_payload({"isError": False}),
        "the converter fabricated an empty success",
    )
    ok = _result_to_payload({"content": [{"type": "text", "text": "{}"}], "isError": False})
    check(
        "a real result is converted unchanged",
        ok.get("content") == [{"type": "text", "text": "{}"}],
        str(ok),
    )


# --------------------------------------------------------------------------- #
# 6. request the gateway's action scopes without administrative access
# --------------------------------------------------------------------------- #


def test_scope_matches_the_live_client() -> None:
    check(
        "the scope set covers the gateway's existing tool actions",
        set(DEFAULT_SCOPE.split()) == {"openid", "offline_access", "kb:read", "kb:contribute", "kb:experience", "kb:export"},
        f"got {DEFAULT_SCOPE!r}",
    )
    check(
        "the scope set asks for no administrative access",
        "kb:admin" not in DEFAULT_SCOPE.split(),
        f"got {DEFAULT_SCOPE!r}",
    )


# --------------------------------------------------------------------------- #
# 7. the backend keeps the broker's contract
# --------------------------------------------------------------------------- #


def test_backend_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        backend = SdkBackend("https://ai-wisdom.example/mcp", tmp)
        for name in ("start", "catalog", "call", "close"):
            check(
                f"the backend exposes {name}()",
                callable(getattr(backend, name, None)),
                "missing",
            )
        check(
            "the state directory is 0700",
            stat.S_IMODE(os.stat(backend.state_dir).st_mode) == 0o700,
            "wrong mode",
        )
        check(
            "the authorize URL file is not world readable",
            not backend._authorize_url_path.exists()
            or stat.S_IMODE(os.stat(backend._authorize_url_path).st_mode) == 0o600,
            "wrong mode",
        )
        check(
            "the connect starts with the modern protocol pin",
            backend.connect_modes[0] == "2026-07-28",
            f"got {backend.connect_modes[0]!r}",
        )


def test_release_catalog() -> None:
    """Synthetic only: no HTTP, live state, callback, or credentials."""
    import hashlib
    import io
    from contextlib import redirect_stderr
    from types import SimpleNamespace
    from unittest.mock import patch

    snapshot = HERE / "tool_catalog.json"
    document = json.loads(snapshot.read_text())
    url = document["mcp_url"]
    with tempfile.TemporaryDirectory() as tmp:
        for name, path, target in (
            ("missing", Path(tmp) / "absent.json", url),
            ("foreign endpoint", snapshot, "https://foreign.invalid/mcp"),
        ):
            check_raises(f"snapshot rejects {name}", SdkTransportError,
                         lambda: SdkBackend(target, Path(tmp) / "unused", tool_catalog=path))
        invalid = Path(tmp) / "invalid.json"
        invalid.write_text("not json")
        check_raises("snapshot rejects malformed bytes", SdkTransportError,
                     lambda: SdkBackend(url, Path(tmp) / "unused", tool_catalog=invalid))
        for name, modify in (
            ("foreign release", lambda d: d.update(server_code_sha="foreign")),
            ("foreign format", lambda d: d.update(schema="foreign")),
            ("missing tool", lambda d: d["tools"].pop()),
            ("duplicate name", lambda d: d["tools"][1].update(name=d["tools"][0]["name"])),
            ("invalid schema", lambda d: d["tools"][0].update(inputSchema=[])),
        ):
            mutated = json.loads(json.dumps(document))
            modify(mutated)
            raw = json.dumps(mutated).encode()
            invalid.write_bytes(raw)
            check_raises(f"snapshot pin rejects {name}", SdkTransportError,
                         lambda: sdk_backend._load_release_catalog(invalid, url))
            # Exercise structural validation independently of the checksum.
            with patch.object(sdk_backend, "RELEASE_CATALOG_SHA256", hashlib.sha256(raw).hexdigest()):
                check_raises(f"snapshot validates {name}", SdkTransportError,
                             lambda: sdk_backend._load_release_catalog(invalid, url))
        check("invalid snapshots never create auth state", not (Path(tmp) / "unused").exists())

    for use_snapshot in (True, False):
        events = []
        owner = []
        class HttpClient:
            def __init__(self, **kwargs):
                check("HTTP retains SDK OAuth provider", isinstance(kwargs["auth"], SdkOAuthProvider))
            async def __aenter__(self):
                owner.append(asyncio.current_task())
                return self
            async def __aexit__(self, *args):
                events.append(("http_exit", asyncio.current_task() is owner[0]))

        class Session:
            def __init__(self, **kwargs):
                # Lifecycle-only double; real SDK cache/validation tested below.
                self.session = SimpleNamespace(_absorb_tool_listing=lambda listing, **kw: listing)
            async def __aenter__(self):
                events.append(("enter", asyncio.current_task() is owner[0]))
                return self
            async def __aexit__(self, *args):
                events.append(("exit", asyncio.current_task() is owner[0]))
            async def list_tools(self):
                events.append(("list", True))
                if use_snapshot:
                    raise AssertionError("snapshot must never call remote tools/list")
                return SimpleNamespace(tools=[SimpleNamespace(model_dump=lambda **kw: tool)
                                              for tool in document["tools"][:1]])
            async def call_tool(self, name, arguments):
                events.append(("call", asyncio.current_task() is owner[0], name, arguments))
                if arguments.get("fail"):
                    raise RuntimeError("synthetic unknown transport outcome")
                return {"content": [{"type": "text", "text": "synthetic-response"}], "isError": False}

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(sdk_backend.httpx2, "AsyncClient", HttpClient), \
                patch.object(sdk_backend, "Client", Session), \
                patch.object(sdk_backend, "streamable_http_client", return_value=object()), \
                redirect_stderr(io.StringIO()) as diagnostics:
            backend = SdkBackend(url, tmp, tool_catalog=snapshot if use_snapshot else None)
            try:
                backend.start()
                check("startup sends no foreground calls", not any(e[0] == "call" for e in events))
                check("catalog source is explicit", backend.catalog_source ==
                      ("release-snapshot" if use_snapshot else "remote"))
                check("catalog preserves complete metadata", backend.catalog() ==
                      (document["tools"] if use_snapshot else document["tools"][:1]))
                result = backend.call(document["tools"][0]["name"], {"synthetic": True})
                check("foreground returns SDK result", result["content"][0]["text"] == "synthetic-response")
                check_raises("unknown result stays transport failure", SdkTransportError,
                             lambda: backend.call(document["tools"][0]["name"], {"fail": True}))
            finally:
                backend.close()
            check("remote list only without snapshot", sum(e[0] == "list" for e in events) == (0 if use_snapshot else 1))
            check("foreground SDK calls never replayed", sum(e[0] == "call" for e in events) == 2)
            check("session lifecycle stays in owner task", all(e[1] for e in events)
                  and [e[0] for e in events].count("exit") == 1
                  and [e[0] for e in events].count("http_exit") == 1
                  and not backend._thread.is_alive())
            if use_snapshot:
                event = json.loads(diagnostics.getvalue())
                check("snapshot readiness claims no authenticated grant",
                      event["authenticated_remote_grant_verified"] is False
                      and event["catalog_sha256"] == sdk_backend.RELEASE_CATALOG_SHA256)


def test_real_sdk_catalog() -> None:
    """Actual Client/session/HTTP transport; only HTTP server replies are fake."""
    import threading
    from concurrent.futures import Future
    from copy import deepcopy
    from unittest.mock import patch
    import httpx2

    # This fixture is deliberately NOT the pinned gateway release artifact.
    fixture = [
        {"name": "schema_probe", "inputSchema": {"type": "object", "properties": {
            "library": {"type": "string", "x-mcp-header": "Library"}}},
         "outputSchema": {"type": "object", "properties": {"count": {"type": "integer"}},
                          "required": ["count"], "additionalProperties": False}},
        {"name": "untyped_probe", "inputSchema": {"type": "object"}},
    ]
    release = sdk_backend._load_release_catalog(
        HERE / "tool_catalog.json", json.loads((HERE / "tool_catalog.json").read_text())["mcp_url"])

    async def exercise(tools, calls, *, incompatible=False):
        requests = []
        original = deepcopy(tools)
        errors = []
        classify = sdk_backend._classify

        def record_error(exc):
            errors.append(exc)
            return classify(exc)

        async def reply(request):
            body = json.loads(request.content)
            requests.append((body, dict(request.headers)))
            if body.get("method") != "tools/call":
                # Immediate failure rather than a fake listing that hides discovery.
                return httpx2.Response(400, json={"jsonrpc": "2.0", "id": body.get("id"),
                    "error": {"code": -32601, "message": "unexpected discovery"}})
            args = body["params"].get("arguments", {})
            result = {"resultType": "complete",
                      "content": [{"type": "text", "text": "synthetic"}], "isError": False}
            if body["params"]["name"] == "schema_probe":
                result["structuredContent"] = {"count": "invalid" if args.get("invalid") else 1}
            return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

        # Avoid any auth-state construction or real grant. Execute production
        # _run_session and _request_loop, including the real SDK context lifecycle.
        backend = SdkBackend.__new__(SdkBackend)
        backend.url = "https://synthetic.invalid/mcp"
        backend._release_catalog = tools
        backend._catalog = []
        backend._ready = threading.Event()
        backend._session_established = False
        backend._queue = asyncio.Queue()
        futures = []
        for name, arguments in calls:
            future = Future()
            futures.append(future)
            backend._queue.put_nowait((name, arguments, future))
        backend._queue.put_nowait(None)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as http:
            try:
                with patch.object(sdk_backend, "_classify", side_effect=record_error):
                    await backend._run_session(http, "2026-07-28")
            except* SdkTransportError as group:
                if not incompatible:
                    raise
                def expected(error):
                    if isinstance(error, BaseExceptionGroup):
                        return all(expected(e) for e in error.exceptions)
                    return (str(error) == "Release tool catalog incompatible with SDK session"
                            and isinstance(error.__cause__, ValueError)
                            and str(error.__cause__) == incompatible)
                if not expected(group):
                    raise
                check("incompatible catalog fails before readiness and HTTP",
                      not backend._ready.is_set() and not backend._session_established
                      and not backend._catalog and not requests)
            else:
                check("compatible real session readiness", not incompatible and backend._ready.is_set())
        check("canonical metadata is never mutated", tools == original)
        return requests, futures, backend, errors

    async def startup(tools, reason):
        """Exercise the outer fallback boundary with real SDK/AnyIO exits."""
        requests, modes = [], []
        real_http, real_client = httpx2.AsyncClient, sdk_backend.Client

        async def reject_http(request):
            requests.append(request)
            return httpx2.Response(400, json={"error": "unexpected HTTP"})

        def http_client(**kwargs):
            return real_http(**kwargs, transport=httpx2.MockTransport(reject_http))

        def client(**kwargs):
            modes.append(kwargs["mode"])
            return real_client(**kwargs)

        # No real grant/state/provider: only the HTTP/auth boundary is replaced.
        backend = SdkBackend.__new__(SdkBackend)
        backend.url = "https://synthetic.invalid/mcp"
        backend.wire_version = sdk_backend.WIRE_VERSION
        backend.connect_modes = sdk_backend.CONNECT_MODES
        backend._release_catalog = deepcopy(tools)
        backend._catalog = []
        backend._ready = threading.Event()
        backend._session_established = False
        backend._mode_used = None
        backend._error = None
        with patch.object(sdk_backend.httpx2, "AsyncClient", side_effect=http_client), \
                patch.object(sdk_backend, "Client", side_effect=client), \
                patch.object(backend, "_provider", return_value=None):
            await backend._serve()
        check(f"startup {reason}: only pinned connect mode", modes == [sdk_backend.CONNECT_MODES[0]])
        check(f"startup {reason}: zero HTTP including negotiation", not requests)
        check(f"startup {reason}: failure signal is not readiness",
              backend._ready.is_set() and backend._error is not None
              and not backend._session_established and not backend._catalog
              and backend._mode_used is None)
        error = backend._error
        check(f"startup {reason}: real SDK grouped local incompatibility",
              isinstance(error, BaseExceptionGroup)
              and error.subgroup(sdk_backend._CatalogIncompatibility) is not None)

    async def grouped_fallback():
        """Mixed groups retain every error; unrelated connect failures still retry."""
        local = sdk_backend._CatalogIncompatibility("synthetic local failure")
        unrelated = ValueError("synthetic unrelated failure")
        mixed = ExceptionGroup("outer", [ExceptionGroup("inner", [local]), unrelated])
        for snapshot, failure, expected_modes in (
            (True, mixed, [sdk_backend.CONNECT_MODES[0]]),
            (True, ExceptionGroup("unrelated", [unrelated]), list(sdk_backend.CONNECT_MODES)),
            (False, mixed, list(sdk_backend.CONNECT_MODES)),
        ):
            modes = []
            backend = SdkBackend.__new__(SdkBackend)
            backend.wire_version = sdk_backend.WIRE_VERSION
            backend.connect_modes = sdk_backend.CONNECT_MODES
            backend._release_catalog = fixture if snapshot else None
            backend._session_established = False
            backend._catalog = []
            backend._ready = threading.Event()
            backend._error = None

            async def fail_session(http, mode):
                modes.append(mode)
                raise failure

            # No SDK/session needed for this exception-identity-only unit check.
            with patch.object(backend, "_provider", return_value=None), \
                    patch.object(backend, "_run_session", side_effect=fail_session):
                await backend._serve()
            check("grouped startup preserves original exception and siblings",
                  backend._error is failure)
            check("only snapshot local incompatibility prevents fallback", modes == expected_modes)

    async def run():
        requests, futures, backend, errors = await exercise(fixture, [
            ("schema_probe", {"library": "synthetic-library"}),
            ("untyped_probe", {}),
            ("schema_probe", {"library": "synthetic-library", "invalid": True}),
            ("schema_probe", {"library": "synthetic-library"}),
        ])
        check("real SDK valid structured result passes", futures[0].exception() is None, repr(errors))
        check("real SDK listed tool without outputSchema passes", futures[1].exception() is None, repr(errors))
        error = futures[2].exception()
        check("real SDK rejects invalid structured result", isinstance(error, SdkTransportError)
              and len(errors) == 1 and isinstance(errors[0], RuntimeError)
              and "Invalid structured content returned by tool schema_probe" in str(errors[0]), repr(errors))
        check("SDK validation failure is not replayed and next call works",
              futures[3].exception() is None and len(requests) == 4)
        check("real first foreground and all calls have no tools/list",
              [r[0]["method"] for r in requests] == ["tools/call"] * 4)
        check("real transport receives SDK x-mcp-header mapping",
              requests[0][1].get("mcp-param-library") == "synthetic-library")
        check("broker and SDK fixture capabilities agree", backend.catalog() == fixture)

        requests, futures, backend, errors = await exercise(release, [(t["name"], {}) for t in release])
        check("all 35 release metadata entries dispatch synthetic replies without discovery",
              len(requests) == 35 and all(f.exception() is None for f in futures)
              and [r[0]["params"]["name"] for r in requests] == [t["name"] for t in release]
              and all(r[0]["method"] == "tools/call" for r in requests), repr(errors))
        check("all 35 release capabilities preserved", backend.catalog() == release)

        bad = deepcopy(fixture)
        bad[0]["inputSchema"]["properties"]["library"]["x-mcp-header"] = "invalid header"
        await exercise(bad, [], incompatible="SDK filtered or changed release tool metadata")
        await startup(bad, "filtered metadata")
        with patch.object(sdk_backend, "distribution_version", return_value="unsupported"):
            await exercise(fixture, [], incompatible="release catalog SDK version mismatch")
            await startup(fixture, "SDK version mismatch")
        # Typed conversion must not silently coerce schema/capability data.
        bad = deepcopy(fixture)
        bad[0]["annotations"] = {"readOnlyHint": "true"}
        await exercise(bad, [], incompatible="SDK cannot preserve release tool metadata")
        await startup(bad, "coerced metadata")
        await grouped_fallback()

    asyncio.run(run())


def main() -> int:
    if "--catalog-only" in sys.argv:
        test_release_catalog()
        test_real_sdk_catalog()
        print(f"passed {len(PASSES)} checks, failed {len(FAILURES)}")
        return 1 if FAILURES else 0
    test_storage_is_protected()
    test_expiry_is_restored()
    test_metadata_validation_fails_closed()
    test_callback_listener()
    test_error_classification()
    test_unsupported_result_fails_closed()
    test_scope_matches_the_live_client()
    test_backend_contract()
    test_release_catalog()
    test_real_sdk_catalog()
    print()
    print(f"passed {len(PASSES)} checks, failed {len(FAILURES)}")
    for failure in FAILURES:
        print(f"  FAILED {failure}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
