"""Bound-browser transport checks with real SQL, no owner credentials."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.access.identity import VerifiedIdentity
from kb.http.auth_routes import create_auth_router
from kb.http.browser_pairing import install_browser_pairing

pytestmark = pytest.mark.integration
ORIGIN = "http://127.0.0.1:8000"


def test_bound_browser_issues_normal_cookie_and_logout_cancels_pending(
    make_store, auth_settings, identity_db
):
    now = dt.datetime.now(dt.UTC)
    store = make_store("bound-browser-http")
    identity = VerifiedIdentity(
        issuer=auth_settings.issuer,
        subject=str(uuid4()),
        principal_id=uuid4(),
        account_id=uuid4(),
        scopes=frozenset({"kb:read"}),
        issued_at=now,
        expires_at=now + dt.timedelta(minutes=5),
        provider_session_id=str(uuid4()),
    )
    with psycopg.connect(identity_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO kb.organisation(id,name) VALUES(%s,%s)",
            (identity.account_id, "bound browser synthetic"),
        )
        conn.execute(
            "INSERT INTO kb.membership"
            "(organisation_id,principal_id,issuer,subject,status) "
            "VALUES(%s,%s,%s,%s,'active')",
            (identity.account_id, identity.principal_id, identity.issuer, identity.subject),
        )
    app = FastAPI()
    app.state.db_connection = lambda: psycopg.connect(identity_db)
    app.include_router(
        create_auth_router(
            flow=SimpleNamespace(
                logout=lambda session, now: store.revoke(session.id, reason="logout", now=now)
            ),
            store=store,
            settings=auth_settings,
            clock=lambda: dt.datetime.now(dt.UTC),
        )
    )
    with httpx.Client() as http:
        install_browser_pairing(app, store, auth_settings, http, None)
        with TestClient(app, base_url=ORIGIN) as client:
            bad = client.post(
                "/auth/agent/begin", json={}, headers={"Origin": "https://foreign.invalid"}
            )
            assert bad.status_code == 403
            bad = client.post(
                "/auth/agent/begin",
                json={"return_to": "//foreign.invalid"},
                headers={"Origin": ORIGIN},
            )
            assert bad.status_code == 400
            begin = client.post(
                "/auth/agent/begin", json={"return_to": "/app/maps"}, headers={"Origin": ORIGIN}
            )
            assert begin.status_code == 201
            assert "httponly" in begin.headers["set-cookie"].lower()
            challenge = begin.json()
            args = {k: challenge[k] for k in ("pairing_id", "nonce")}
            assert (
                client.post(
                    "/auth/agent/complete", json=args, headers={"Origin": ORIGIN}
                ).status_code
                == 202
            )
            assert store.approve_browser_pairing(
                args["pairing_id"],
                args["nonce"],
                identity,
                authority_scopes=identity.scopes,
                authority_cloud_client=False,
                credential_expires_at=identity.expires_at,
                now=now,
            )
            with TestClient(app, base_url=ORIGIN) as stranger:
                assert (
                    stranger.post(
                        "/auth/agent/complete", json=args, headers={"Origin": ORIGIN}
                    ).status_code
                    == 403
                )
            complete = client.post("/auth/agent/complete", json=args, headers={"Origin": ORIGIN})
            assert complete.status_code == 200 and complete.json()["authenticated"] is True
            assert complete.json()["safe_return_to"] == "/app/maps"
            cookie_header = complete.headers.get_list("set-cookie")[-1].lower()
            max_age = int(cookie_header.split("max-age=", 1)[1].split(";", 1)[0])
            assert 0 < max_age <= 300
            session = client.get("/auth/session").json()
            assert session["authenticated"] is True and session["principal_id"] == str(
                identity.principal_id
            )
            assert (
                client.post("/auth/agent/begin", json={}, headers={"Origin": ORIGIN}).status_code
                == 409
            )
            assert client.post("/auth/logout").status_code == 403
            csrf = client.cookies.get("kb_csrf")
            assert client.post("/auth/logout", headers={"x-kb-csrf": csrf}).status_code == 200
            assert client.get("/auth/session").json()["authenticated"] is False
            # Signed-out logout also cancels only this browser's pending challenge.
            second = client.post("/auth/agent/begin", json={}, headers={"Origin": ORIGIN}).json()
            assert client.post("/auth/logout").status_code == 200
            assert not store.approve_browser_pairing(
                second["pairing_id"],
                second["nonce"],
                identity,
                authority_scopes=identity.scopes,
                authority_cloud_client=False,
                credential_expires_at=identity.expires_at,
                now=now,
            )
