"""Talking to the **running** Keycloak, without the agent ever seeing a secret.

C07 task 1. The C07 stubs were written in a workspace with no JVM. On the
owner's host runs real Keycloak, so the tests that can be answered by the
running server are answered by the running server. This module is the only
place that reaches for it.

The credential rule, and how this module keeps it
-------------------------------------------------
The bootstrap admin credentials live in
``/home/kb-inspect/ai-wisdom-config/keycloak.env`` on the host. They are read
**inside the test process**, by :func:`load_host_env`, and merged into
``os.environ``. Nothing from that file is ever returned, logged, repr'd,
written to a report, or placed on a command line: the values live in this
process's memory for the length of a test and go nowhere else. Token and
secret values are wrapped in :class:`Secret`, whose ``__repr__`` and
``__str__`` are a character count, so a stray ``print``, a f-string, a pytest
assertion message or a traceback cannot leak one.

Loopback versus the public issuer
---------------------------------
Admin REST is read over ``http://127.0.0.1:19281`` and the discovery/JWKS
documents are read from the public issuer. That is not two servers. The
Keycloak process is configured with its external hostname, so the discovery
document it serves — including on loopback — advertises the public
``https://id.ai-wisdom...`` issuer and the public ``jwks_uri``. Loopback is
therefore the same realm, the same keys and the same configuration as the
public route, reached without putting Caddy and TLS in the path of a read.
The test asserts that equality rather than assuming it.

What this module is allowed to change
-------------------------------------
One thing: a confidential client named ``test-e03-*`` that it creates itself
and deletes by exact id. No realm setting, no user, no role, no policy, no
session of anybody else is touched. Deletion is always by the exact internal
id of an object this run created; there is no bulk delete anywhere in here.
"""

from __future__ import annotations

import os
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

#: Where the host keeps the Keycloak bootstrap credentials. Overridable for a
#: different host; read in-process, never echoed.
DEFAULT_ENV_FILE: Final[str] = "/home/kb-inspect/ai-wisdom-config/keycloak.env"
ENV_FILE_VAR: Final[str] = "KB_C07_KEYCLOAK_ENV"

#: Admin REST of the same Keycloak process that serves the public issuer.
DEFAULT_ADMIN_BASE: Final[str] = "http://127.0.0.1:19281"
ADMIN_BASE_VAR: Final[str] = "KB_C07_KEYCLOAK_ADMIN_BASE"

#: The issuer the product is configured against, and the gateway's own
#: redirect URI, which the realm must know byte-for-byte.
DEFAULT_ISSUER: Final[str] = "https://id.ai-wisdom.150-241-115-134.sslip.io/realms/knowledge-hub"
ISSUER_VAR: Final[str] = "KB_C07_ISSUER"
GATEWAY_REDIRECT_URI: Final[str] = "https://ai-wisdom.150-241-115-134.sslip.io/auth/callback"

REALM: Final[str] = "knowledge-hub"
MASTER_REALM: Final[str] = "master"
PRODUCT_CLIENT: Final[str] = "kb-gateway"

#: Every object this card creates carries this prefix (C07 global constraints).
SYNTHETIC_PREFIX: Final[str] = "test-e03-"

#: Attribute keys stamped on every object created here, so a later reader can
#: tell a synthetic fixture from a product object without guessing.
ATTR_CARD: Final[str] = "c07.card"
ATTR_TASK: Final[str] = "c07.task"
ATTR_RUN_ID: Final[str] = "c07.run_id"
ATTR_SYNTHETIC: Final[str] = "c07.synthetic"


class LiveKeycloakUnavailable(RuntimeError):
    """No live Keycloak could be reached, or the host env is missing.

    Raised as a failure, never converted into a skip: a check that did not run
    is ``not_run``, and the project treats a silent skip as a false green.
    """


class Secret:
    """A credential that must not reach a log, a repr or an assertion message.

    ``reveal()`` is the only way out and it is called in exactly two places:
    the ``client_secret`` form field and the admin password form field.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"<secret redacted, {len(self._value)} chars>"

    __str__ = __repr__


@dataclass(frozen=True)
class SyntheticClient:
    """A fixture object this run created. These ids are what a run must report."""

    client_id: str
    internal_id: str
    mapper_internal_id: str
    run_id: str
    secret: Secret = field(repr=False)


def load_host_env(path: str | None = None) -> str:
    """Merge the host's Keycloak env file into ``os.environ``, in this process.

    Returns the path that was read and nothing else. An already-exported
    variable wins over the file, so a caller can supply one without editing
    the host's configuration.
    """
    target = pathlib.Path(path or os.environ.get(ENV_FILE_VAR) or DEFAULT_ENV_FILE)
    if not target.is_file():
        raise LiveKeycloakUnavailable(
            f"no Keycloak env file at {target}; set {ENV_FILE_VAR} to point at it"
        )
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and value:
            os.environ.setdefault(name, value)
    return str(target)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise LiveKeycloakUnavailable(f"{name} is not set in the environment")
    return value


class LiveKeycloak:
    """A thin, honest client for the running server. Read, plus one fixture."""

    def __init__(
        self,
        *,
        env_file: str | None = None,
        admin_base: str | None = None,
        issuer: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.env_file = load_host_env(env_file)
        self.admin_base = (
            admin_base or os.environ.get(ADMIN_BASE_VAR) or DEFAULT_ADMIN_BASE
        ).rstrip("/")
        self.issuer = (issuer or os.environ.get(ISSUER_VAR) or DEFAULT_ISSUER).rstrip("/")
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)
        self._admin: Secret | None = None
        self._admin_expiry: float = 0.0

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> LiveKeycloak:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"LiveKeycloak(admin_base={self.admin_base!r}, issuer={self.issuer!r})"

    # -- plumbing -------------------------------------------------------

    def _post_form(self, url: str, form: dict[str, str]) -> httpx.Response:
        return self._http.post(
            url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    @staticmethod
    def _fail(response: httpx.Response, what: str) -> LiveKeycloakUnavailable:
        # The ``error`` field is a fixed OAuth vocabulary. The description is
        # not read: a provider is free to echo a request value back in it, and
        # one of the request values here is a secret.
        try:
            code = response.json().get("error", "unknown")
        except Exception:  # any non-JSON body is just "unknown"
            code = "non_json_body"
        return LiveKeycloakUnavailable(f"{what} failed: HTTP {response.status_code} {code}")

    def admin_token(self) -> Secret:
        """A master-realm admin token, for the Admin REST API.

        Keycloak issues these with the standard ``admin-cli`` password grant,
        which is the bootstrap mechanism and is not something this module
        switches on. It authenticates as the realm administrator; it is used
        here to read the realm and to create and delete this run's own
        synthetic client, and for nothing else.
        """
        if self._admin is not None and time.monotonic() < self._admin_expiry:
            return self._admin
        password = Secret(_require("KC_BOOTSTRAP_ADMIN_PASSWORD"))
        response = self._post_form(
            f"{self.admin_base}/realms/{MASTER_REALM}/protocol/openid-connect/token",
            {
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": _require("KC_BOOTSTRAP_ADMIN_USERNAME"),
                "password": password.reveal(),
            },
        )
        if response.status_code != 200:
            raise self._fail(response, "admin token request")
        document = response.json()
        self._admin = Secret(document["access_token"])
        # Refresh well before the observed 60 s lifetime; a 401 mid-test would
        # otherwise look like a product failure.
        self._admin_expiry = time.monotonic() + 30.0
        return self._admin

    def admin_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = self.admin_token()
        return self._http.request(
            method,
            f"{self.admin_base}{path}",
            headers={"Authorization": f"Bearer {token.reveal()}"},
            **kwargs,
        )

    def admin_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.admin_request(method, path, **kwargs)
        if response.status_code >= 400:
            raise self._fail(response, f"admin {method} {path}")
        return response.json() if response.content else None

    # -- reading --------------------------------------------------------

    def realm(self) -> dict[str, Any]:
        """The current realm representation, read back from the running server."""
        return self.admin_json("GET", f"/admin/realms/{REALM}")

    def clients(self) -> list[dict[str, Any]]:
        return self.admin_json("GET", f"/admin/realms/{REALM}/clients")

    def discovery(self, *, public: bool = True) -> dict[str, Any]:
        """The OIDC discovery document.

        ``public=True`` reads the real issuer, which is what a token's ``iss``
        claim is compared against. ``public=False`` reads the same document off
        loopback, which is how the two routes are shown to be one server.
        """
        base = self.issuer if public else f"{self.admin_base}/realms/{REALM}"
        response = self._http.get(f"{base}/.well-known/openid-configuration")
        if response.status_code != 200:
            raise self._fail(response, "discovery")
        return response.json()

    def jwks(self, document: dict[str, Any] | None = None) -> dict[str, Any]:
        url = (document or self.discovery())["jwks_uri"]
        response = self._http.get(url)
        if response.status_code != 200:
            raise self._fail(response, "jwks")
        return response.json()

    # -- the one synthetic fixture ---------------------------------------

    def create_service_client(self, run_id: str, name: str) -> SyntheticClient:
        """Create a confidential, service-account-only client named ``test-e03-*``.

        No browser flow, no implicit flow, no password grant, no device code,
        no redirect URI: it is a fixture for one token request and it can reach
        nothing the product uses. An audience mapper points the token at its
        own client id, because Keycloak otherwise puts only ``account`` in
        ``aud`` and leaves the client id in ``azp``.
        """
        if not name.startswith(SYNTHETIC_PREFIX):
            raise ValueError(f"a synthetic object must be named {SYNTHETIC_PREFIX}*, got {name!r}")
        representation = {
            "clientId": name,
            "name": f"C07 synthetic service client (run {run_id})",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "redirectUris": [],
            "webOrigins": [],
            "attributes": {
                ATTR_CARD: "C07",
                ATTR_TASK: "task-1",
                ATTR_RUN_ID: run_id,
                ATTR_SYNTHETIC: "true",
                "access.token.lifespan": "300",
            },
        }
        created = self.admin_request("POST", f"/admin/realms/{REALM}/clients", json=representation)
        if created.status_code != 201:
            raise self._fail(created, "create synthetic client")
        internal_id = _id_from(created)
        if not internal_id:
            raise LiveKeycloakUnavailable("Keycloak created the client but returned no Location id")

        mapper_response = self.admin_request(
            "POST",
            f"/admin/realms/{REALM}/clients/{internal_id}/protocol-mappers/models",
            json={
                "name": "c07-self-audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.client.audience": name,
                    "id.token.claim": "false",
                    "access.token.claim": "true",
                    "introspection.token.claim": "true",
                    "lightweight.claim": "false",
                },
            },
        )
        if mapper_response.status_code != 201:
            raise self._fail(mapper_response, "create synthetic audience mapper")
        mapper_id = _id_from(mapper_response)

        secret_document = self.admin_request(
            "POST", f"/admin/realms/{REALM}/clients/{internal_id}/client-secret"
        )
        if secret_document.status_code != 200:
            raise self._fail(secret_document, "generate synthetic client secret")
        return SyntheticClient(
            client_id=name,
            internal_id=internal_id,
            mapper_internal_id=mapper_id,
            run_id=run_id,
            secret=Secret(secret_document.json()["value"]),
        )

    def delete_client_by_id(self, internal_id: str) -> int:
        """Delete exactly one client, by the exact id this run created.

        There is no query, no filter and no sweep here. A client this run did
        not create cannot be reached through this method, because the id is
        the value this run recorded.
        """
        response = self.admin_request("DELETE", f"/admin/realms/{REALM}/clients/{internal_id}")
        if response.status_code not in (204, 200, 404):
            raise self._fail(response, "delete synthetic client")
        return response.status_code

    # -- the token -------------------------------------------------------

    def client_credentials_token(self, client: SyntheticClient) -> Secret:
        response = self._post_form(
            f"{self.admin_base}/realms/{REALM}/protocol/openid-connect/token",
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": client.secret.reveal(),
            },
        )
        if response.status_code != 200:
            raise self._fail(response, "client_credentials token request")
        return Secret(response.json()["access_token"])


def _id_from(response: httpx.Response) -> str:
    """Keycloak answers a create with 201, an empty body and a Location header."""
    location = response.headers.get("location", "")
    if location:
        return location.rsplit("/", 1)[-1]
    try:
        return str(response.json().get("id", ""))
    except Exception:  # no id is a clean, reportable outcome
        return ""


def new_run_id() -> str:
    """A short run id, stamped on every object this run creates."""
    return uuid.uuid4().hex[:8]
