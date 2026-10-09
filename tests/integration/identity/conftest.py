"""Shared fixtures for the C07 identity integration tests.

Two real things and one stand-in, named so nobody has to guess which is which:

* **Real PostgreSQL 16.2** from the ``pg_server`` fixture, with the migrations
  applied to a dedicated ``kb_identity`` database. A separate database rather
  than a second schema application in the shared one: ``CREATE POLICY`` and
  ``CREATE TRIGGER`` are not idempotent, and re-applying 0001-0003 to the same
  database aborts halfway with an error that names a policy instead of the
  real cause. The dedicated database also keeps these tests from colliding with
  the rows C03/C06 leave behind.

* **Real RSA keys and a real JWKS document**, generated per session with
  ``cryptography`` and signed with RS256 by PyJWT. Nothing about the
  signature path is mocked: a token that fails these tests would fail against
  Keycloak for the same reason.

* **A local OIDC provider process** — a real HTTP server on 127.0.0.1 that
  serves discovery, JWKS, ``/authorize`` and ``/token``, and mints genuinely
  signed ID tokens.

  This is **NOT Keycloak**. Keycloak is not installed in this workspace (no
  JVM; see ``docs/handoff/runtime-capabilities.json``), and pretending
  otherwise would be the single most misleading thing this test suite could
  do. What this stand-in exercises for real: the discovery/issuer check, the
  JWKS fetch, RS256 verification, the authorization-code + PKCE sequence, and
  every refusal the gateway makes. What it cannot exercise: realm import,
  interactive login, MFA, Keycloak's own quirks, and the MCP audience mapper.
  That gap is recorded as ``not_run`` behind E03 in
  ``docs/handoff/results/C07.json`` and as a skipped test in
  ``test_keycloak_live.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kb.access.identity import IdentitySettings, TokenVerifier
from kb.access.session import Sealer, SessionStore
from kb.http.auth_config import AuthSettings

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = sorted((ROOT / "migrations").glob("0*.sql"))
IDENTITY_DB = "kb_identity"

UTC = dt.UTC
EXPIRY = 300


# ------------------------------------------------------------------ database


def _retarget(uri: str, database: str) -> str:
    """Point an existing libpq URI at a different database, keeping the socket.

    ``pgserver`` hands out ``postgresql://user:@/postgres?host=/tmp/...`` —
    authority, then the database, then the query. Only the middle part moves.
    """
    head, scheme, tail = uri.partition("://")
    authority, slash, rest = tail.partition("/")
    _old_database, question, params = rest.partition("?")
    assert scheme and slash, uri
    suffix = f"?{params}" if question else ""
    return f"{head}://{authority}/{database}{suffix}"


def _psql(dsn: str, sql: str) -> tuple[int, str]:
    from pgserver.postgres_server import POSTGRES_BIN_PATH

    proc = subprocess.run(  # noqa: S603 - absolute path to the bundled psql
        [str(POSTGRES_BIN_PATH / "psql"), dsn, "-v", "ON_ERROR_STOP=1", "--tuples-only"],
        input=sql.encode(),
        capture_output=True,
        timeout=60,
    )
    return proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")


@pytest.fixture(scope="session")
def identity_db(pg_server, psql_strict):  # pg_server owns the server lifetime
    """A real PostgreSQL database with 0001-0003 applied, once per session."""
    _rc, out = psql_strict(f"DROP DATABASE IF EXISTS {IDENTITY_DB};")
    rc, out = psql_strict(f"CREATE DATABASE {IDENTITY_DB};")
    if rc != 0:
        raise RuntimeError("could not create the identity database:\n" + out[-2000:])

    dsn = _retarget(pg_server.get_uri(), IDENTITY_DB)
    rc, out = _psql(dsn, "".join(m.read_text(encoding="utf-8") for m in MIGRATIONS))
    if rc != 0:
        raise RuntimeError("identity migration failed:\n" + out[-2000:])
    return dsn


# ------------------------------------------------------------------- crypto


@dataclass
class SigningKey:
    """One real RSA key, in both the JWK form a provider publishes and the PEM
    form a signer needs."""

    kid: str
    private_pem: bytes
    private_key: rsa.RSAPrivateKey

    @classmethod
    def generate(cls, kid: str) -> SigningKey:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return cls(kid=kid, private_pem=pem, private_key=key)

    @property
    def public_pem(self) -> bytes:
        return self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def jwk(self) -> dict[str, Any]:
        raw = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private_key.public_key()))
        raw.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return raw

    def sign(
        self,
        claims: dict[str, Any],
        *,
        algorithm: str = "RS256",
        key: bytes | None = None,
        headers: dict[str, Any] | None = None,
    ) -> str:
        head = {"kid": self.kid}
        head.update(headers or {})
        return jwt.encode(claims, key or self.private_pem, algorithm=algorithm, headers=head)


@pytest.fixture(scope="session")
def signing_keys() -> dict[str, SigningKey]:
    """Two keys so rotation and wrong-key rejection are both expressible."""
    return {
        "current": SigningKey.generate("c07-key-current"),
        "retired": SigningKey.generate("c07-key-retired"),
        "attacker": SigningKey.generate("c07-key-attacker"),
    }


# ------------------------------------------------- local OIDC provider (NOT Keycloak)


@dataclass
class _AuthCode:
    redirect_uri: str
    code_challenge: str
    client_id: str
    subject: str
    nonce: str | None
    account_id: str | None


@dataclass
class LocalOidcProvider:
    """A real HTTP process speaking enough OIDC to exercise the gateway.

    Labelled honestly: this stands in for the *network behaviour* of Keycloak,
    not for Keycloak. It exists because Keycloak cannot be run here, and the
    alternatives — mocking ``httpx``, or skipping the flow tests — would leave
    the authorization-code path unverified.
    """

    host: str
    port: int
    client_id: str = "kb-gateway"
    #: A fixture credential for a process that listens on loopback and exists
    #: for one test session. It is not a Keycloak secret and grants nothing.
    client_secret: str = "c07-local-provider-fixture-secret"  # noqa: S105 - test fixture, not a credential
    #: Stands in for the `kb_account_id` user attribute that the realm's
    #: protocol mapper puts in the ID token. Every test user has one.
    default_account_id: str = "c0700000-0000-4000-8000-0000000000bb"
    keys: dict[str, SigningKey] = field(default_factory=dict)
    codes: dict[str, _AuthCode] = field(default_factory=dict)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    jwks_generation: int = 0
    forced_issuer: str | None = None
    key_order: list[str] = field(default_factory=list)

    @property
    def issuer(self) -> str:
        return f"http://{self.host}:{self.port}/realms/knowledge-hub"

    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def jwks_document(self) -> dict[str, Any]:
        order = self.key_order or list(self.keys)
        return {"keys": [self.keys[kid].jwk() for kid in order]}

    def discovery_document(self) -> dict[str, Any]:
        base = self.base()
        realm = f"{base}/realms/knowledge-hub/protocol/openid-connect"
        return {
            "issuer": self.forced_issuer or self.issuer,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{realm}/token",
            "jwks_uri": f"{realm}/certs",
            "userinfo_endpoint": f"{realm}/userinfo",
            "end_session_endpoint": f"{base}/logout",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
        }

    def mint_id_token(
        self,
        *,
        subject: str,
        nonce: str | None,
        audience: str | None = None,
        issuer: str | None = None,
        account_id: str | None = None,
        expires_in: int = EXPIRY,
        scopes: str | None = None,
        extra: dict[str, Any] | None = None,
        key: SigningKey | None = None,
        headers: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": issuer or self.issuer,
            "sub": subject,
            "aud": audience or self.client_id,
            "iat": now,
            "exp": now + expires_in,
            "jti": f"c07-{now}-{len(self.token_requests)}",
            "typ": "ID",
            "session_state": f"kc-session-{subject}",
            "preferred_username": f"user-{subject[:8]}",
        }
        if nonce is not None:
            claims["nonce"] = nonce
        if account_id is not None:
            claims["kb_account_id"] = account_id
        if scopes is not None:
            claims["scope"] = scopes
        claims.update(extra or {})
        return (key or self.keys[self.key_order[0]]).sign(claims, headers=headers)


def _make_handler(provider: LocalOidcProvider) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep the pytest output clean and, more importantly, keep
            # authorization codes out of stderr.
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, document: dict[str, Any]) -> None:
            self._send(status, json.dumps(document).encode(), "application/json")

        def do_GET(self) -> None:  # BaseHTTPRequestHandler API
            parts = urlsplit(self.path)
            if parts.path in {
                "/.well-known/openid-configuration",
                # the path a real Keycloak realm serves it from
                "/realms/knowledge-hub/.well-known/openid-configuration",
            }:
                self._json(200, provider.discovery_document())
                return
            if parts.path == "/realms/knowledge-hub/protocol/openid-connect/certs":
                # max-age=0 would defeat the cache; use a long one and let the
                # unknown-kid path be what tests rotation.
                self.send_response(200)
                body = json.dumps(provider.jwks_document()).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "max-age=600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if parts.path == "/authorize":
                self._authorize(parse_qs(parts.query))
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # BaseHTTPRequestHandler API
            parts = urlsplit(self.path)
            if not parts.path.endswith("/token"):
                self._json(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
            provider.token_requests.append(form)
            self._token(form)

        # -- the two endpoints that matter -------------------------------

        def _authorize(self, query: dict[str, list[str]]) -> None:
            def one(name: str) -> str:
                return query.get(name, [""])[0]

            if one("client_id") != provider.client_id:
                self._json(400, {"error": "unauthorized_client"})
                return
            if one("code_challenge_method") != "S256":
                # A provider that offers `plain` would be a downgrade this
                # gateway must never accept, so the stand-in refuses it too.
                self._json(400, {"error": "invalid_request", "error_description": "S256 only"})
                return
            code = f"code-{len(provider.codes) + 1}-{one('state')[:8]}"
            provider.codes[code] = _AuthCode(
                redirect_uri=one("redirect_uri"),
                code_challenge=one("code_challenge"),
                client_id=one("client_id"),
                subject=one("login_hint") or "c0700000-0000-4000-8000-000000000001",
                nonce=one("nonce") or None,
                account_id=one("account_hint") or provider.default_account_id,
            )
            location = f"{one('redirect_uri')}?{urlencode({'code': code, 'state': one('state')})}"
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _token(self, form: dict[str, str]) -> None:
            import base64
            import hashlib

            if form.get("grant_type") != "authorization_code":
                self._json(400, {"error": "unsupported_grant_type"})
                return
            if form.get("client_id") != provider.client_id:
                self._json(401, {"error": "invalid_client"})
                return
            if form.get("client_secret") != provider.client_secret:
                self._json(401, {"error": "invalid_client"})
                return
            code = form.get("code", "")
            record = provider.codes.get(code)
            if record is None:
                self._json(400, {"error": "invalid_grant"})
                return
            del provider.codes[code]
            if form.get("redirect_uri") != record.redirect_uri:
                self._json(400, {"error": "invalid_grant", "error_description": "redirect_uri"})
                return
            expected = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(form.get("code_verifier", "").encode("ascii")).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            if expected != record.code_challenge:
                self._json(400, {"error": "invalid_grant", "error_description": "PKCE"})
                return
            id_token = provider.mint_id_token(
                subject=record.subject, nonce=record.nonce, account_id=record.account_id
            )
            self._json(
                200,
                {
                    "access_token": "not-verified-by-this-card",
                    "token_type": "Bearer",
                    "expires_in": EXPIRY,
                    "id_token": id_token,
                },
            )

    return Handler


@pytest.fixture(scope="session")
def provider(signing_keys) -> LocalOidcProvider:
    instance = LocalOidcProvider(host="127.0.0.1", port=0, keys=dict(signing_keys))
    instance.key_order = ["current", "retired"]
    server = ThreadingHTTPServer((instance.host, 0), _make_handler(instance))
    instance.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------------- the gateway


@pytest.fixture
def identity_settings(provider: LocalOidcProvider) -> IdentitySettings:
    return IdentitySettings(
        issuer=provider.issuer, audience=provider.client_id, client_id=provider.client_id
    )


@pytest.fixture
def verifier(identity_settings, provider) -> TokenVerifier:
    with httpx.Client(timeout=5.0) as client:
        yield TokenVerifier(identity_settings, client)


@pytest.fixture
def auth_settings(provider: LocalOidcProvider) -> AuthSettings:
    return AuthSettings(
        issuer=provider.issuer,
        audience=provider.client_id,
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        identity_key=Sealer.generate(),
        redirect_uri="http://127.0.0.1:8000/auth/callback",
        cookie_secure=False,
    )


@pytest.fixture
def sealer() -> Sealer:
    return Sealer(Sealer.generate())


@pytest.fixture
def make_store(identity_db, sealer):
    """Build a SessionStore. Each call is an independent gateway instance."""

    def _make(instance_id: str, **kwargs) -> SessionStore:
        return SessionStore(identity_db, instance_id=instance_id, sealer=sealer, **kwargs)

    return _make


@pytest.fixture
def store(make_store) -> SessionStore:
    return make_store("gateway-a")
