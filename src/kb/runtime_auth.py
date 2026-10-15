"""Assemble existing identity libraries and the request admission adapter."""

from __future__ import annotations

import datetime as dt
import os
import socket

import httpx
from fastapi import FastAPI, Request
from psycopg_pool import ConnectionPool

from kb.access.identity import IdentitySettings, TokenVerifier, VerifiedIdentity
from kb.access.logout import LogoutVerifier
from kb.access.membership_keycloak import KeycloakSessionRevoker
from kb.access.session import Sealer, SessionStore
from kb.http import libraries, members
from kb.http.auth_config import AuthSettings
from kb.http.auth_flow import AuthorizationCodeFlow
from kb.http.auth_routes import create_auth_router
from kb.http.backchannel import create_backchannel_router
from kb.runtime_identity import create_resolver


def install_auth(
    app: FastAPI, pool: ConnectionPool, http_client: httpx.Client, settings: AuthSettings
) -> None:
    store = SessionStore(
        os.environ["KB_DATABASE_URL"],
        instance_id=os.environ.get("KB_INSTANCE_ID", socket.gethostname()),
        sealer=Sealer(settings.identity_key),
        idle_minutes=settings.session_idle_minutes,
        absolute_hours=settings.session_absolute_hours,
        login_ttl_minutes=settings.login_ttl_minutes,
    )

    def coordinates(audience: str, client_id: str, *, parties=frozenset()) -> IdentitySettings:
        return IdentitySettings(
            issuer=settings.issuer,
            audience=audience,
            client_id=client_id,
            authorized_parties=parties,
            required_scopes=settings.required_scopes
            if client_id != settings.client_id
            else frozenset(),
            jwks_url=settings.jwks_url,
            discovery_url=settings.discovery_url,
            clock_skew_seconds=settings.clock_skew_seconds,
        )

    browser = TokenVerifier(coordinates(settings.client_id, settings.client_id), http_client)
    bearer = TokenVerifier(
        coordinates(
            os.environ["KB_MCP_AUDIENCE"],
            os.environ["KB_MCP_CLIENT_ID"],
            parties=frozenset(
                os.environ.get("KB_MCP_ALLOWED_CLIENT_IDS", os.environ["KB_MCP_CLIENT_ID"]).split()
            ),
        ),
        http_client,
    )
    flow = AuthorizationCodeFlow(
        settings=settings, verifier=browser, store=store, client=http_client
    )
    app.include_router(
        create_auth_router(
            flow=flow, store=store, settings=settings, clock=lambda: dt.datetime.now(dt.UTC)
        )
    )
    app.include_router(
        create_backchannel_router(
            verifier=LogoutVerifier(coordinates(settings.client_id, settings.client_id), browser),
            store=store,
            clock=lambda: dt.datetime.now(dt.UTC),
        )
    )
    resolve, resolve_token = create_resolver(pool, store, settings, bearer)
    app.state.resolve_principal = resolve
    app.state.resolve_token = resolve_token
    app.state.session_store = store
    app.state.auth_settings = settings
    app.state.browser_verifier = browser
    app.state.bearer_verifier = bearer
    app.dependency_overrides[libraries.current_principal] = resolve

    def verified_identity(request: Request) -> VerifiedIdentity:
        resolve(request)
        return request.state.identity

    app.dependency_overrides[members.current_identity] = verified_identity
    if os.environ.get("KB_KC_REVOKER_CLIENT_ID"):
        app.state.session_revoker = KeycloakSessionRevoker(
            base_url=os.environ["KB_KC_REVOKER_BASE_URL"],
            realm="knowledge-hub",
            client_id=os.environ["KB_KC_REVOKER_CLIENT_ID"],
            client_secret=os.environ["KB_KC_REVOKER_CLIENT_SECRET"],
        )
