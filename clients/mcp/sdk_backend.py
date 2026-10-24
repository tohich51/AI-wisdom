"""SDK backend for the owner-local MCP broker.

Same contract as ``native_backend.Backend`` (``start``/``catalog``/``call``/
``close``), but the upstream conversation runs over the official MCP Python
SDK (mcp 2.3.0) with this client's own OAuth grant.

What the acceptance contract depends on, and where it lives here:

* One event loop for the whole lifetime.  A single lifetime coroutine owns the
  HTTP client, the transport and the client session, and it is also the task
  that executes the tool calls.  The anyio task groups inside the transport
  must be entered and exited by the same task, so the broker's synchronous
  bridge only hands work to that coroutine and waits for the answer.
* One HTTP client and one auth context for the lifetime.
* The grant belongs to this client.  Tokens, the client registration and the
  discovered authorization-server metadata live in a 0700 directory as a 0600
  file.  Nothing is read from the Codex credential store and no token, secret
  or book content is ever printed.
* The SDK's own provider performs discovery, PKCE, refresh and the 401
  re-authorisation.  A refresh only happens for a request that was rejected
  with 401 before execution; a timeout or a broken write is never replayed.
* The stored token's expiry and the discovered authorization-server metadata
  are restored on start, so a restart refreshes against the real Keycloak
  token endpoint instead of the resource server's origin.  Restored metadata is
  validated (RFC 8707 resource match, RFC 8414 issuer, token-endpoint binding)
  and a mismatch fails closed instead of silently falling back.
* The modern protocol version is pinned. By default, real ``tools/list``
  discovers the catalog. Explicit release-snapshot mode uses pinned public
  metadata instead, without verifying a remote grant at startup. Foreground
  calls still use SDK OAuth and server authorization. Connect-only failures
  may try auto negotiation; after local readiness no tool call is replayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp import Client
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.oauth2 import OAuthFlowError
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    issuers_match,
    validate_metadata_issuer,
)
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from mcp.shared.exceptions import MCPError
from mcp.types import ListToolsResult
from pydantic import AnyUrl

from graph_wire import decode_result
from transport_errors import NativeRequestError

# The scope this client asks for.
#
# The gateway declares these four action scopes for its existing tools.
# They must be requested explicitly: openid alone admits the transport but
# does not authorize a tool call. Library membership and RLS still decide
# which information the authenticated owner may access on every call.
DEFAULT_SCOPE = "openid offline_access kb:read kb:contribute kb:experience kb:export"

# Discovered authorization-server metadata is metadata, not a cached answer.
# It may be reused for a restart-driven refresh only while it is fresh.
METADATA_TTL_SECONDS = 24 * 60 * 60

# A token that is about to expire is refreshed a little early, so a request is
# never sent with a token that dies on the way.
EXPIRY_SKEW_SECONDS = 30.0

CALL_TIMEOUT_SECONDS = 90.0
START_TIMEOUT_SECONDS = 180.0
CALLBACK_TIMEOUT_SECONDS = 600.0

WIRE_HEADER = "x-kb-mcp-wire"
WIRE_VERSION = "xz-base64-v1"

AUTHORIZE_URL_PATH_NAME = "authorize-url.txt"
OAUTH_STATE_PATH_NAME = "oauth-state.json"

# Modern protocol version.  The mounted gateway is dual-era and speaks this
# version; pinning it costs no negotiation round trip.  The second entry is
# the SDK's own auto negotiation, used only when the pinned modern envelope
# is refused by the live mount.
CONNECT_MODES = ("2026-07-28", "auto")


class SdkTransportError(RuntimeError):
    """The transport failed.  The result of the request is unknown.

    Fail closed: the request is not replayed and it is not handed to another
    backend.
    """


class _CatalogIncompatibility(SdkTransportError):
    """Local release metadata cannot be installed; never negotiate around it."""


# --------------------------------------------------------------------------- #
# protected token / registration / metadata store
# --------------------------------------------------------------------------- #


class ProtectedTokenStorage:
    """Persistent OAuth state for this client only.

    The directory is 0700 and the file is 0600.  The stored grant is this
    client's own: it is created by this module's authorization flow and is
    never imported from another agent's credential store.
    """

    def __init__(self, directory, client_id, redirect_uri, scope, issuer, resource=None):
        self.directory = Path(directory).absolute()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / OAUTH_STATE_PATH_NAME
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.scope = scope
        self.issuer = issuer
        self._lock = threading.Lock()
        self.resource = resource
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None
        self._expires_at: float | None = None
        self._metadata: dict[str, Any] | None = None
        self._load()

    # -- persistence ------------------------------------------------------- #

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        tokens = raw.get("tokens")
        expected = {"client_id": self.client_id, "issuer": self.issuer,
                    "scope": self.scope, "redirect_uri": self.redirect_uri,
                    "resource": self.resource}
        if any(raw.get(key) != value for key, value in expected.items()):
            raise OAuthFlowError("Stored OAuth state belongs to a different client configuration")
        if isinstance(tokens, dict):
            try:
                self._tokens = OAuthToken.model_validate(tokens)
            except Exception:
                self._tokens = None
        client_info = raw.get("client_info")
        if isinstance(client_info, dict):
            try:
                self._client_info = OAuthClientInformationFull.model_validate(client_info)
            except Exception:
                self._client_info = None
        expires_at = raw.get("expires_at")
        if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
            self._expires_at = float(expires_at)
        metadata = raw.get("metadata")
        if isinstance(metadata, dict):
            self._metadata = metadata

    def _persist(self) -> None:
        payload = {
            "version": 1,
            "client_id": self.client_id,
            "issuer": self.issuer,
            "scope": self.scope,
            "resource": getattr(self, "resource", None),
            "redirect_uri": self.redirect_uri,
            "tokens": self._tokens.model_dump(mode="json") if self._tokens else None,
            "client_info": (
                self._client_info.model_dump(mode="json", by_alias=True, exclude_none=True)
                if self._client_info
                else None
            ),
            "expires_at": self._expires_at,
            "metadata": self._metadata,
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # -- TokenStorage protocol -------------------------------------------- #

    async def get_tokens(self) -> OAuthToken | None:
        with self._lock:
            return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        with self._lock:
            self._tokens = tokens
            expires_in = tokens.expires_in
            if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
                self._expires_at = time.time() + float(expires_in)
            else:
                self._expires_at = None
            self._persist()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        with self._lock:
            return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            self._client_info = client_info
            self._persist()

    # -- extras used by the provider adapter ------------------------------ #

    async def get_token_expiry(self) -> float | None:
        with self._lock:
            return self._expires_at

    async def get_cached_metadata(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._metadata) if self._metadata else None

    async def set_cached_metadata(self, metadata: dict[str, Any]) -> None:
        with self._lock:
            self._metadata = metadata
            self._persist()

    # -- helpers ----------------------------------------------------------- #

    def seed_client_info(self) -> OAuthClientInformationFull:
        """Return the public client registration this backend uses.

        The realm's public client is reused for this client's own PKCE grant:
        the client id is a public identifier, while the grant, the tokens and
        the refresh token are this client's own.
        """
        info = OAuthClientInformationFull(
            client_id=self.client_id,
            redirect_uris=[AnyUrl(self.redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=self.scope,
            client_name="ai-wisdom-sdk-backend",
            issuer=self.issuer,
        )
        with self._lock:
            self._client_info = info
            self._persist()
        return info


# --------------------------------------------------------------------------- #
# provider adapter
# --------------------------------------------------------------------------- #


def _metadata_is_fresh(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    fetched_at = metadata.get("fetched_at")
    if not isinstance(fetched_at, (int, float)) or isinstance(fetched_at, bool):
        return False
    return (time.time() - float(fetched_at)) <= METADATA_TTL_SECONDS


class SdkOAuthProvider(OAuthClientProvider):
    """Official provider plus a narrow restore-and-validate adapter.

    The SDK's ``_initialize`` restores the tokens and the client registration
    but not the token expiry, and it does not restore the discovered
    authorization-server metadata.  Without both, a restart treats an expired
    token as valid (a 401 then forces a full browser authorization) and a
    refresh would be sent to the resource server's origin instead of to the
    authorization server.  This adapter restores both.

    Every metadata path is validated before it is used:

    * the protected-resource metadata must name a resource that matches the
      server URL (RFC 8707, the SDK's own ``_validate_resource_match``),
    * the authorization-server metadata must name the expected issuer
      (RFC 8414 section 3.3, ``validate_metadata_issuer``),
    * the token endpoint must live on the issuer's own origin, so a refresh
      can never be addressed to a foreign host.

    A validation failure raises: the flow stops instead of quietly falling
    back to the resource server's origin.
    """

    def __init__(self, *args, pinned_scope: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pinned_scope = pinned_scope

    # -- restore ----------------------------------------------------------- #

    async def _initialize(self) -> None:
        self._overlap_validated_endpoint = None
        await super()._initialize()
        storage = self.context.storage
        expiry = await storage.get_token_expiry()  # type: ignore[attr-defined]
        if expiry is not None:
            # Refresh slightly before the real expiry instead of after it.
            self.context.token_expiry_time = expiry - EXPIRY_SKEW_SECONDS
        tokens = self.context.current_tokens
        if tokens is None or not tokens.refresh_token:
            return
        # A stored token may need a refresh.  The refresh must go to the
        # authorization server, never to the resource server's origin.
        cached = await storage.get_cached_metadata()  # type: ignore[attr-defined]
        overlap_restored_metadata = _metadata_is_fresh(cached)
        if not overlap_restored_metadata:
            cached = await self._discover()
        if not cached:
            # Fail closed: without a validated authorization server the SDK
            # would send the refresh to the resource server's origin.  Drop the
            # stored grant instead, so the standard 401 flow re-authorizes.
            self.context.clear_tokens()
            return
        await self._apply_metadata(cached)
        # Only restored, validated metadata: discovery is not a warmup trigger.
        if overlap_restored_metadata:
            self._overlap_validated_endpoint = str(self.context.oauth_metadata.token_endpoint)

    # -- validation -------------------------------------------------------- #

    def _configured_issuer(self) -> str | None:
        """The issuer this client is configured to trust."""
        issuer = getattr(self.context.storage, "issuer", None)
        if isinstance(issuer, str) and issuer:
            return issuer.rstrip("/")
        return None

    def _select_authorization_server(self, advertised: list[str]) -> str:
        configured = self._configured_issuer()
        if not configured or not any(issuers_match(configured, item) for item in advertised):
            raise OAuthFlowError("Protected resource does not advertise the configured issuer")
        return next(item for item in advertised if issuers_match(configured, item))

    def _expected_issuer(self) -> str:
        configured = self._configured_issuer()
        if not configured or not issuers_match(configured, super()._expected_issuer()):
            raise OAuthFlowError("Discovered authorization server differs from configured issuer")
        return configured

    async def _apply_metadata(self, metadata: dict[str, Any]) -> None:
        """Validate and install cached authorization-server metadata.

        Raises ``OAuthFlowError`` when the cached metadata does not belong to
        this server/issuer.  Silently ignoring it would make the SDK fall back
        to the resource server's origin for a refresh.
        """
        from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

        prm = metadata.get("protected_resource")
        asm = metadata.get("authorization_server")
        auth_server_url = metadata.get("auth_server_url")
        if not isinstance(prm, dict) or not isinstance(asm, dict) or not isinstance(auth_server_url, str):
            raise OAuthFlowError("Cached authorization metadata is malformed")
        try:
            prm_model = ProtectedResourceMetadata.model_validate(prm)
            asm_model = OAuthMetadata.model_validate(asm)
        except Exception as exc:
            raise OAuthFlowError("Cached authorization metadata is malformed") from exc

        discovered = auth_server_url.rstrip("/")
        configured = self._configured_issuer()
        if configured and not issuers_match(configured, discovered):
            # The cached metadata belongs to another authorization server.
            raise OAuthFlowError(
                "Cached authorization metadata names a different issuer than the configured one"
            )
        # RFC 8414 section 3.3 / SEP-2468: the metadata must name the issuer.
        validate_metadata_issuer(asm_model, discovered)
        self._check_token_endpoint(asm_model, discovered)
        # RFC 8707: the resource must match the server this client talks to.
        await self._validate_resource_match(prm_model)
        self.context.protected_resource_metadata = prm_model
        self.context.oauth_metadata = asm_model
        self.context.auth_server_url = discovered

    @staticmethod
    def _check_token_endpoint(asm, expected_issuer: str) -> None:
        """The token endpoint must live on the issuer's own origin."""
        token_endpoint = getattr(asm, "token_endpoint", None)
        if token_endpoint is None:
            raise OAuthFlowError("Authorization server metadata has no token endpoint")
        endpoint = urlparse(str(token_endpoint))
        issuer = urlparse(expected_issuer)
        if endpoint.scheme != issuer.scheme or endpoint.netloc != issuer.netloc:
            raise OAuthFlowError(
                "Authorization server token endpoint is not bound to the expected issuer"
            )

    # -- discovery --------------------------------------------------------- #

    async def _discover(self) -> dict[str, Any] | None:
        """Discover and validate the protected-resource and AS metadata."""
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(15.0)) as client:
            prm = None
            for url in build_protected_resource_metadata_discovery_urls(None, self.context.server_url):
                try:
                    response = await client.get(url)
                except Exception:
                    continue
                prm = await handle_protected_resource_response(response)
                if prm is not None:
                    break
            if prm is None:
                return None
            # RFC 8707: the resource must match the server this client talks to.
            await self._validate_resource_match(prm)

            auth_server_url = self._select_authorization_server(
                [str(item) for item in prm.authorization_servers]
            ).rstrip("/")
            asm = None
            for url in build_oauth_authorization_server_metadata_discovery_urls(
                auth_server_url, self.context.server_url
            ):
                try:
                    response = await client.get(url)
                except Exception:
                    continue
                ok, candidate = await handle_auth_metadata_response(response)
                if ok and candidate is not None:
                    asm = candidate
                    break
            if asm is None:
                return None
            # RFC 8414 section 3.3 / SEP-2468: the metadata must name the issuer.
            validate_metadata_issuer(asm, auth_server_url)
            self._check_token_endpoint(asm, auth_server_url)

            metadata = {
                "fetched_at": time.time(),
                "auth_server_url": auth_server_url,
                "protected_resource": prm.model_dump(mode="json"),
                "authorization_server": asm.model_dump(mode="json"),
            }
            await self.context.storage.set_cached_metadata(metadata)  # type: ignore[attr-defined]
            return metadata

    # -- persistence of what the SDK discovered --------------------------- #

    async def _handle_token_response(self, response) -> None:
        """Persist the metadata the SDK discovered during the first flow.

        The SDK's own flow discovers the protected-resource and
        authorization-server metadata and then throws it away at process
        exit.  Without this the next cold start would have to discover it
        again before it may refresh.
        """
        await super()._handle_token_response(response)
        await self._persist_context_metadata()

    async def _handle_refresh_response(self, response) -> bool:
        handled = await super()._handle_refresh_response(response)
        if handled:
            await self._persist_context_metadata()
        return handled

    async def _persist_context_metadata(self) -> None:
        prm = self.context.protected_resource_metadata
        asm = self.context.oauth_metadata
        auth_server_url = self.context.auth_server_url
        if prm is None or asm is None or not auth_server_url:
            return
        metadata = {
            "fetched_at": time.time(),
            "auth_server_url": auth_server_url.rstrip("/"),
            "protected_resource": prm.model_dump(mode="json"),
            "authorization_server": asm.model_dump(mode="json"),
        }
        await self.context.storage.set_cached_metadata(metadata)  # type: ignore[attr-defined]

    # -- scope ------------------------------------------------------------- #

    async def _perform_authorization(self):
        """Authorize with this client's own scope set.

        The provider otherwise replaces the requested scope with every scope
        the protected-resource metadata advertises, which would ask for more
        than the live client has.  This client asks for exactly the live
        client's scope set.
        """
        expected = self._expected_issuer()
        if self.context.oauth_metadata is None:
            raise OAuthFlowError("Validated authorization metadata is required")
        validate_metadata_issuer(self.context.oauth_metadata, expected)
        self._check_token_endpoint(self.context.oauth_metadata, expected)
        pinned = self.pinned_scope
        previous = self.context.client_metadata.scope
        if pinned:
            self.context.client_metadata.scope = pinned
        try:
            return await super()._perform_authorization()
        finally:
            self.context.client_metadata.scope = previous


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #


def _result_to_payload(result: Any) -> dict[str, Any]:
    """Convert an SDK tool result into the broker's result shape.

    An unsupported or malformed result fails closed with
    ``SdkTransportError``.  An empty success is never fabricated: a caller
    could not tell such a payload apart from a real read, and the broker
    would then serve an empty result as if the upstream had answered.
    """
    if hasattr(result, "model_dump"):
        payload = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    elif isinstance(result, dict):
        payload = dict(result)
    else:
        raise SdkTransportError("Upstream returned an unsupported tool result")
    if not isinstance(payload, dict):
        raise SdkTransportError("Upstream returned an unsupported tool result")
    if not isinstance(payload.get("content"), list):
        raise SdkTransportError("Upstream tool result carried no content list")
    payload.setdefault("isError", False)
    return payload


def _classify(exception: BaseException) -> BaseException:
    """Map an SDK/transport exception onto the broker's two error kinds."""
    if isinstance(exception, NativeRequestError):
        return exception
    if isinstance(exception, MCPError):
        # The upstream answered with a protocol-level error: the transport is
        # alive, so the broker keeps serving.
        return NativeRequestError("Upstream rejected the request")
    if isinstance(exception, (asyncio.TimeoutError, httpx2.TimeoutException)):
        return SdkTransportError("Upstream request timed out; the result is unknown")
    if isinstance(exception, httpx2.HTTPError):
        return SdkTransportError("Upstream transport failed; the result is unknown")
    if isinstance(exception, (ConnectionError, OSError)):
        return SdkTransportError("Upstream transport failed; the result is unknown")
    return SdkTransportError("Upstream call failed; the result is unknown")


# Release metadata only: these pins confer no grant or server-side permission.
RELEASE_CATALOG_SHA256 = "33a52dfca53820eba540774fe33b94cdc8e0d6266b1e7fbbfadb65380e36ed28"
RELEASE_SERVER_SHA = "66a174cc6b4fb31ff30d99e7cfb799ab28279271"
RELEASE_CATALOG_SDK_VERSION = "2.3.0"


def _load_release_catalog(path, url):
    """Fail closed on anything other than this release's public tool metadata.

    A rollout must update the artifact and its source pins together. This is
    not remote version negotiation and says nothing about OAuth or live ACLs.
    """
    try:
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != RELEASE_CATALOG_SHA256:
            raise ValueError("release catalog checksum mismatch")
        document = json.loads(raw)
        if (document["schema"] != "ai-wisdom-release-tool-catalog/v1"
                or document["server_code_sha"] != RELEASE_SERVER_SHA
                or document["mcp_url"] != url):
            raise ValueError("release catalog identity mismatch")
        tools = document["tools"]
        if not isinstance(tools, list) or len(tools) != 35:
            raise ValueError("release catalog must contain 35 tools")
        names = []
        for tool in tools:
            if (not isinstance(tool, dict)
                    or not isinstance(tool.get("name"), str) or not tool["name"]
                    or not isinstance(tool.get("inputSchema"), dict)
                    or tool["inputSchema"].get("type") != "object"):
                raise ValueError("invalid release tool metadata")
            names.append(tool["name"])
        if len(set(names)) != len(names):
            raise ValueError("duplicate release tool names")
        return tools  # Preserve all schema fields and capability metadata.
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SdkTransportError("Invalid or missing release tool catalog") from exc


def _install_release_catalog(client, tools):
    """Install public metadata, never authorization or cached tool results.

    Version-pinned seam: mcp 2.3.0 Client.list_tools cache-hit path in
    mcp/client/client.py:965-970 uses this same session method. It installs
    output schemas (including None) AND x-mcp-header maps without discovery.
    The SDK mutates/filters its input; use an independent typed copy and
    reject any loss or change instead of advertising a different universe.
    """
    try:
        if distribution_version("mcp") != RELEASE_CATALOG_SDK_VERSION:
            raise ValueError("release catalog SDK version mismatch")
        listing = ListToolsResult.model_validate({"tools": deepcopy(tools)})
        def records(value):
            return [tool.model_dump(by_alias=True, mode="json", exclude_unset=True)
                    for tool in value.tools]
        if records(listing) != tools:
            raise ValueError("SDK cannot preserve release tool metadata")
        effective = client.session._absorb_tool_listing(listing, complete=True)
        if effective.next_cursor is not None or records(effective) != tools:
            raise ValueError("SDK filtered or changed release tool metadata")
    except Exception as exc:
        raise _CatalogIncompatibility("Release tool catalog incompatible with SDK session") from exc


class _RefreshOverlap:
    """Opt-in public connection preparation, on this client's own pool.

    Request hooks observe the SDK's yielded refresh request AFTER restored
    metadata validation. No auth-flow replacement, HTTP at startup, or replay.
    A raw Request deliberately avoids client defaults (headers/cookies/params).
    """

    TIMEOUT = 2.0

    def __init__(self, client, provider, url, *, enabled=False):
        self.client = client
        self.provider = provider
        self.enabled = enabled
        self.task = None
        self.started = False
        self.target = None
        self.health = None
        if enabled:
            target = httpx2.URL(url)
            if target.scheme != "https" or not target.host or target.username or target.password:
                raise ValueError("Refresh overlap requires a credential-free HTTPS MCP URL")
            self.target = target
            self.health = target.copy_with(path="/healthz", query=None, fragment=None)

    async def __aenter__(self):
        if self.enabled:
            self.client.event_hooks["request"].append(self.on_request)
        return self

    async def __aexit__(self, *exc):
        if self.enabled:
            self.client.event_hooks["request"].remove(self.on_request)
        if self.task is not None:
            self.task.cancel()
            # No background task may outlive this HTTP client. The warm task
            # handles its own cancellation and its response-close deadline.
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def on_request(self, request):
        if asyncio.current_task() is self.task:
            return
        if request.url == self.target and self.task is not None:
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            return
        if self.started or request.method != "POST":
            return
        endpoint = getattr(self.provider, "_overlap_validated_endpoint", None)
        if endpoint is None or request.url != httpx2.URL(endpoint):
            return
        # SDK 2.3.0 builds a buffered form request; do not read/log credentials
        # or infer any destination from this body. Only its grant type matters.
        if request.headers.get("content-type") != "application/x-www-form-urlencoded":
            return
        if not request.content.startswith(b"grant_type=refresh_token&"):
            return
        self.started = True
        self.task = asyncio.create_task(self._warm())

    async def _warm(self):
        response = None
        try:
            async with asyncio.timeout(self.TIMEOUT):
                request = httpx2.Request(
                    "GET", self.health,
                    extensions={"timeout": httpx2.Timeout(self.TIMEOUT).as_dict()},
                )
                response = await self.client.send(
                    request, auth=None, follow_redirects=False, stream=True,
                )
                async for _ in response.aiter_raw():
                    pass
        except (Exception, asyncio.CancelledError):
            # Preparation is never a reason to fail, retry or fall back on a
            # tool call. Parent-call cancellation still propagates via shield.
            pass
        finally:
            if response is not None:
                try:
                    await asyncio.wait_for(response.aclose(), timeout=0.5)
                except (Exception, asyncio.CancelledError):
                    pass


class SdkBackend:
    """The SDK backend, shaped like ``native_backend.Backend``."""

    refresh_overlap = False  # Also preserves isolated __new__ lifecycle fixtures.

    def __init__(
        self,
        url,
        state_dir,
        client_id="kb-mcp",
        scope=DEFAULT_SCOPE,
        redirect_uri="http://127.0.0.1:19289/callback/sdk-backend",
        issuer=None,
        callback_port=19289,
        callback_path="/callback/sdk-backend",
        callback_timeout=CALLBACK_TIMEOUT_SECONDS,
        call_timeout=CALL_TIMEOUT_SECONDS,
        start_timeout=START_TIMEOUT_SECONDS,
        wire_version=WIRE_VERSION,
        connect_modes=CONNECT_MODES,
        tool_catalog=None,
        refresh_overlap=False,
    ):
        for name, value in (("url", url), ("issuer", issuer)):
            parsed = urlparse(value) if isinstance(value, str) else None
            if (parsed is None or parsed.scheme != "https" or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError(f"{name} must be an explicit credential-free HTTPS URL")
        self.url = url
        self.refresh_overlap = refresh_overlap
        self._release_catalog = (
            _load_release_catalog(tool_catalog, url) if tool_catalog is not None else None
        )
        self.catalog_source = "release-snapshot" if tool_catalog is not None else "remote"
        self.state_dir = Path(state_dir).absolute()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.client_id = client_id
        self.scope = scope
        self.redirect_uri = redirect_uri
        self.issuer = issuer
        self.callback_port = callback_port
        self.callback_path = callback_path
        self.callback_timeout = callback_timeout
        self.call_timeout = call_timeout
        self.start_timeout = start_timeout
        self.wire_version = wire_version
        self.connect_modes = tuple(connect_modes)
        self.storage = ProtectedTokenStorage(
            self.state_dir / "oauth", client_id, redirect_uri, scope, issuer, resource=url
        )
        self.storage.seed_client_info()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._catalog: list[dict[str, Any]] = []
        self._error: BaseException | None = None
        self._mode_used: str | None = None
        self._session_established = False
        self._authorize_url_path = self.state_dir / AUTHORIZE_URL_PATH_NAME
        self._callback_server = None
        self._callback_future: asyncio.Future | None = None

    # -- OAuth plumbing ---------------------------------------------------- #

    async def _ensure_callback_server(self) -> None:
        """Bring the loopback listener up before any browser is opened.

        The redirect can only arrive after the browser was sent to the
        authorization page, so the port must already be served by then.
        """
        if self._callback_server is not None:
            return
        loop = asyncio.get_running_loop()
        self._callback_future = loop.create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._handle_callback_request(reader, writer)

        self._callback_server = await asyncio.start_server(
            handle, "127.0.0.1", self.callback_port
        )

    async def _handle_callback_request(self, reader, writer) -> None:
        """Serve one loopback request; only the exact callback path is served."""
        try:
            request_line = await reader.readline()
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            method = parts[0] if parts else ""
            target = parts[1] if len(parts) > 1 else ""
            parsed = urlparse(target)
            query = parse_qs(parsed.query)

            if parsed.path == "/done":
                body = b"<!doctype html><html><body>AI-wisdom: authorization received. You can close this tab.</body></html>"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
                )
                await writer.drain()
                return

            if method != "GET" or parsed.path != self.callback_path:
                body = b"AI-wisdom: invalid callback request."
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
                )
                await writer.drain()
                return

            code = (query.get("code") or [None])[0]
            state = (query.get("state") or [None])[0]
            iss = (query.get("iss") or [None])[0]
            if not code or not state:
                body = b"AI-wisdom: the authorization response carried no code."
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
                )
                await writer.drain()
                if self._callback_future is not None and not self._callback_future.done():
                    self._callback_future.set_exception(
                        OAuthFlowError("authorization response carried no code")
                    )
                return

            # Clean redirect: the code must not stay in the address bar.
            location = f"http://127.0.0.1:{self.callback_port}/done"
            body = b"<!doctype html><html><body>AI-wisdom: authorization received.</body></html>"
            writer.write(
                b"HTTP/1.1 303 See Other\r\nLocation: " + location.encode() + b"\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
            if self._callback_future is not None and not self._callback_future.done():
                self._callback_future.set_result({"code": code, "state": state, "iss": iss})
        except Exception:
            if self._callback_future is not None and not self._callback_future.done():
                self._callback_future.set_exception(OAuthFlowError("callback listener failed"))
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _redirect_handler(self, url: str) -> None:
        """Publish the authorization URL and open it in the owner's browser.

        The loopback listener is already up when this runs, so the redirect
        that comes back cannot arrive before the port is served.  The URL is
        written to a 0600 file inside the private state directory; only the
        path is logged.
        """
        await self._ensure_callback_server()
        fd = os.open(str(self._authorize_url_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(url)
        print(
            json.dumps({"event": "oauth_authorize_url", "path": str(self._authorize_url_path)}),
            file=sys.stderr,
            flush=True,
        )
        try:
            subprocess.run(
                ["open" if sys.platform == "darwin" else "xdg-open", url],
                check=False,
                timeout=15,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    async def _callback_handler(self) -> AuthorizationCodeResult:
        """Wait for the loopback redirect and return the authorization code."""
        await self._ensure_callback_server()
        assert self._callback_future is not None
        try:
            query = await asyncio.wait_for(self._callback_future, timeout=self.callback_timeout)
        finally:
            await self._close_callback_server()
        return AuthorizationCodeResult(
            code=query["code"], state=query["state"], iss=query.get("iss")
        )

    async def _close_callback_server(self) -> None:
        server = self._callback_server
        self._callback_server = None
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass

    # -- lifetime ---------------------------------------------------------- #

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        try:
            headers = {WIRE_HEADER: self.wire_version}
            timeout = httpx2.Timeout(30.0, read=120.0)
            provider = self._provider()
            async with httpx2.AsyncClient(
                auth=provider, timeout=timeout, headers=headers
            ) as client:
                async with _RefreshOverlap(
                    client, provider, getattr(self, "url", None), enabled=self.refresh_overlap
                ):
                    last_error: BaseException | None = None
                    for mode in self.connect_modes:
                        try:
                            await self._run_session(client, mode)
                            return
                        except Exception as exc:  # noqa: BLE001 - retried with the next mode
                            last_error = exc
                            if self._release_catalog is not None and (
                                isinstance(exc, SdkTransportError)
                                or (isinstance(exc, BaseExceptionGroup)
                                    and exc.subgroup(_CatalogIncompatibility) is not None)
                            ):
                                # AnyIO can wrap local incompatibility on context exit.
                                # Keep the original group (including unrelated failures),
                                # but never negotiate/discover around invalid metadata.
                                raise
                            if self._session_established:
                                # The session was live: a user call may have been
                                # sent, so the result is unknown.  Never rebuild
                                # the session here and never replay the call.
                                raise
                            # Connect-time failure only: no user call was sent,
                            # so the next mode may be tried.
                            self._catalog = []
                            continue
                    raise last_error if last_error else RuntimeError("connect failed")
        except BaseException as exc:  # noqa: BLE001 - the broker needs the reason
            self._error = exc
            self._ready.set()

    async def _run_session(self, client: httpx2.AsyncClient, mode: str) -> None:
        """One connect attempt: transport, session, catalog, then the calls.

        The transport is handed to the client as a context manager, so the
        client enters and exits it inside this one task: the anyio task
        groups it owns are never entered by one task and exited by another.
        """
        modern = Client(
            server=streamable_http_client(self.url, http_client=client),
            mode=mode,
            read_timeout_seconds=120.0,
        )
        async with modern:
            if self._release_catalog is not None:
                _install_release_catalog(modern, self._release_catalog)
                self._catalog = self._release_catalog
            else:
                listing = await modern.list_tools()
                self._catalog = [
                    tool.model_dump(by_alias=True, mode="json", exclude_none=True)
                    for tool in listing.tools
                ]
            self._mode_used = mode
            self._session_established = True
            self._ready.set()
            await self._request_loop(modern)

    def _provider(self) -> SdkOAuthProvider:
        metadata = OAuthClientMetadata(
            client_name="ai-wisdom-sdk-backend",
            redirect_uris=[AnyUrl(self.redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=self.scope,
        )
        return SdkOAuthProvider(
            server_url=self.url,
            client_metadata=metadata,
            storage=self.storage,
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
            pinned_scope=self.scope,
        )

    async def _request_loop(self, client: Client) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:
                break
            tool, arguments, future = item
            try:
                result = await client.call_tool(tool, arguments)
                payload = _result_to_payload(result)
                if tool == "get_semantic_network":
                    payload = decode_result(payload)
                future.set_result(payload)
            except BaseException as exc:  # noqa: BLE001 - classified below
                future.set_exception(_classify(exc))

    # -- broker-facing API -------------------------------------------------- #

    def start(self) -> None:
        """Wait for local session readiness, not proof of a remote grant.

        Snapshot mode defers the first authenticated request to the foreground
        SDK call. The remote-discovery mode still calls tools/list at startup.
        """
        if self._release_catalog is not None:
            print(json.dumps({
                "event": "sdk_catalog", "source": self.catalog_source,
                "server_code_sha": RELEASE_SERVER_SHA,
                "catalog_sha256": RELEASE_CATALOG_SHA256,
                "authenticated_remote_grant_verified": False,
            }), file=sys.stderr, flush=True)
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self._serve()), name="ai-wisdom-sdk-backend", daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout=self.start_timeout):
            raise SdkTransportError("SDK backend start timed out")
        if self._error is not None:
            raise SdkTransportError(f"SDK backend start failed: {type(self._error).__name__}")
        if not self._catalog:
            raise SdkTransportError("SDK backend start produced no tool catalog")

    def catalog(self) -> list[dict[str, Any]]:
        return list(self._catalog)

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one tool and wait for its result."""
        if self._error is not None:
            raise SdkTransportError("SDK backend is not running")
        if self._loop is None or self._queue is None:
            raise SdkTransportError("SDK backend is not running")
        future: Future = Future()
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (tool, arguments, future))
        except RuntimeError as exc:
            raise SdkTransportError("SDK backend is not running") from exc
        try:
            return future.result(timeout=self.call_timeout)
        except FutureTimeout as exc:
            raise SdkTransportError("Upstream request timed out; the result is unknown") from exc

    def close(self) -> None:
        """Close the session from the task that owns it."""
        if self._loop is not None and self._queue is not None:
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=15)
