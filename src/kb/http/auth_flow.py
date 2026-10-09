"""C07 — the browser's authorization-code flow.

This is the *relying party* half of OIDC and nothing more. The authorization
server is Keycloak; this module only sends the browser there, receives the
code back, redeems it once, and turns the ID token into a durable session.
There is no grant issuance, no user store and no token minting anywhere in
this repository — a gateway that issued its own tokens would be a second,
unreviewed identity provider.

What is delegated rather than written here
------------------------------------------
* every cryptographic and claim-level check, in :mod:`kb.access.identity`
  (PyJWT);
* the discovery and JWKS fetches, in the same module (httpx);
* session persistence and CSRF comparison, in :mod:`kb.access.session`.

What remains here is the protocol sequence itself: build the authorization
request, POST the code to the token endpoint, check the result. That is the
part that is genuinely the gateway's, and it is short on purpose. If Authlib
is later added to the lock (see ``docs/handoff/results/C07.json``), this
module is the only thing that changes.

Why PKCE is always ``S256``
---------------------------
RFC 7636 §4.2, and there is no code path that offers ``plain``: an intercepted
authorization code is useless without the verifier, and the verifier exists
only in the browser that started this flow and in the sealed column of one
``auth_login_transaction`` row.

What is logged here, and what is not
---------------------------------
The audit trail records the outcome, a fixed reason code, the principal, the
session and the gateway instance. It never records the authorization code, the
state, the PKCE verifier, the client secret or a cookie value.
:func:`kb.access.identity.redact` is what stands between a credential and a
log line, and ``tests/integration/identity/test_browser_flow.py`` turns the
root logger to DEBUG and checks that the whole login still produces nothing
replayable. (The `httpx`/`httpcore` loggers were checked for the same reason
and found not to print bodies at any level, so no suppression of them is
shipped — a control with a made-up rationale is worse than no control.)
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from kb.access.identity import IdentityRejected, TokenVerifier, redact
from kb.access.session import (
    IssuedSession,
    LoginTransaction,
    LoginTransactionInvalid,
    Session,
    SessionStore,
    code_challenge_for,
    new_browser_binding,
    new_code_verifier,
    new_nonce,
    new_state,
)
from kb.http.auth_config import AuthSettings, safe_return_target

_log = logging.getLogger("kb.auth")


class LoginRejected(Exception):
    """The login could not be completed. ``code`` is safe to return."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LoginRequest:
    authorization_url: str
    state: str
    browser_binding: str
    return_to: str | None


@dataclass(frozen=True)
class LoginCompleted:
    issued: IssuedSession
    return_to: str | None


class AuthorizationCodeFlow:
    """One instance per gateway process. Holds no session state itself."""

    def __init__(
        self,
        *,
        settings: AuthSettings,
        verifier: TokenVerifier,
        store: SessionStore,
        client: httpx.Client,
    ) -> None:
        self._settings = settings
        self._verifier = verifier
        self._store = store
        self._client = client

    # -- step 1: send the browser to the provider ------------------------

    def start(self, *, return_to: str | None, now: dt.datetime) -> LoginRequest:
        target = safe_return_target(return_to)
        state = new_state()
        binding = new_browser_binding()
        nonce = new_nonce()
        verifier = new_code_verifier()

        self._store.open_login(
            state=state,
            binding=binding,
            code_verifier=verifier,
            code_challenge=code_challenge_for(verifier),
            redirect_uri=self._settings.redirect_uri,
            return_to=target,
            nonce=nonce,
            now=now,
        )

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.client_id,
                "redirect_uri": self._settings.redirect_uri,
                "scope": self._settings.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge_for(verifier),
                "code_challenge_method": "S256",
            }
        )
        endpoint = self._verifier.endpoint("authorization_endpoint")
        separator = "&" if "?" in endpoint else "?"
        return LoginRequest(
            authorization_url=f"{endpoint}{separator}{query}",
            state=state,
            browser_binding=binding,
            return_to=target,
        )

    # -- step 2: redeem the code ----------------------------------------

    def complete(
        self, *, code: str | None, state: str | None, binding: str | None, now: dt.datetime
    ) -> LoginCompleted:
        if not code or not state:
            raise LoginRejected("callback_parameters_missing")

        try:
            transaction = self._store.consume_login(state, binding=binding, now=now)
        except LoginTransactionInvalid as exc:
            # A failed match is the normal case for a replayed or forged
            # callback. It is a refusal, not an outage, and the reason code
            # deliberately does not distinguish "expired" from "wrong
            # browser" from "already used".
            _log.warning(
                "auth.callback.rejected", extra={"event": "callback_rejected", "reason": "state"}
            )
            raise LoginRejected(exc.args[0]) from exc

        token = self._redeem(code, transaction)
        try:
            identity = self._verifier.verify(
                token, expected_nonce=transaction.nonce, require_scopes=False
            )
        except IdentityRejected as exc:
            _log.warning(
                "auth.callback.rejected",
                extra={"event": "callback_rejected", "reason": exc.code},
            )
            raise LoginRejected(exc.code) from exc

        issued = self._store.create_session(identity, now=now)
        _log.info(
            "auth.login.ok",
            extra={
                "event": "login_established",
                "principal_id": str(identity.principal_id),
                "session_id": str(issued.session.id),
                "gateway_instance": self._store.instance_id,
            },
        )
        return LoginCompleted(issued=issued, return_to=transaction.return_to)

    def _redeem(self, code: str, transaction: LoginTransaction) -> str:
        """POST the code to the provider's token endpoint and return the ID token.

        ``client_secret_post`` rather than HTTP Basic: both are standard,
        Keycloak accepts both for a confidential client, and posting keeps the
        secret out of a request line that a proxy might log. Whatever the
        provider answers, only one field name is read.
        """
        endpoint = self._verifier.endpoint("token_endpoint")
        try:
            response = self._client.post(
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": transaction.redirect_uri,
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                    "code_verifier": transaction.code_verifier,
                },
                headers={"accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise LoginRejected("token_endpoint_unreachable") from exc

        if response.status_code != 200:
            _log.warning(
                "auth.token_exchange.rejected",
                extra={
                    "event": "token_exchange_rejected",
                    "provider_status": response.status_code,
                    "code": redact(code),
                },
            )
            raise LoginRejected("token_exchange_rejected")
        try:
            payload = response.json()
        except ValueError as exc:
            raise LoginRejected("token_response_malformed") from exc
        if not isinstance(payload, dict):
            raise LoginRejected("token_response_malformed")
        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            # The gateway authenticates the browser session with the ID token.
            # An access token here means the provider ignored
            # response_type=code, and accepting it would mean trusting a
            # token whose audience is some API rather than this client.
            raise LoginRejected("id_token_missing")
        return id_token

    # -- step 3: end the session -----------------------------------------

    def logout(self, session: Session, *, now: dt.datetime) -> bool:
        revoked = self._store.revoke(session.id, reason="logout", now=now)
        _log.info(
            "auth.logout",
            extra={
                "event": "logout",
                "session_id": str(session.id),
                "principal_id": str(session.principal_id),
                "revoked": revoked,
            },
        )
        return revoked
