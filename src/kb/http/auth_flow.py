"""C07 — the browser's authorization-code flow.

Relying-party half of OIDC: send the browser to the provider, redeem the
code once, and turn the ID token into a durable session. Authlib builds the
authorization URL, PKCE challenge and token request; this module owns the
sequence, the shared httpx client and the decision to refuse.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import httpx
from authlib.oauth2.client import OAuth2Client
from authlib.oauth2.rfc6749.errors import OAuth2Error

from kb.access.identity import IdentityRejected, TokenVerifier
from kb.access.session import (
    IssuedSession,
    LoginTransaction,
    LoginTransactionInvalid,
    Session,
    SessionMissing,
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

    def _protocol_client(self) -> OAuth2Client:
        """A throwaway Authlib client bound to the shared httpx pool.

        One per protocol step. It carries a token only for the duration of a
        single ``fetch_token`` call, so nothing survives to be reused.
        """
        return OAuth2Client(
            session=self._client,
            client_id=self._settings.client_id,
            client_secret=self._settings.client_secret,
            scope=self._settings.scopes,
            redirect_uri=self._settings.redirect_uri,
            code_challenge_method="S256",
        )

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

        endpoint = self._verifier.endpoint("authorization_endpoint")
        # Authlib derives the challenge from the verifier and picks S256 from
        # ``code_challenge_method``; there is no ``plain`` path to take.
        url, _state = self._protocol_client().create_authorization_url(
            endpoint,
            state=state,
            code_verifier=verifier,
            nonce=nonce,
        )
        return LoginRequest(
            authorization_url=url,
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

        try:
            issued = self._store.create_session(identity, now=now)
        except SessionMissing:
            raise LoginRejected("provider_session_logged_out") from None
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

    def _check_answer(self, response: httpx.Response) -> httpx.Response:
        """Refuse anything that is not a 200 carrying a JSON object.

        Registered as an ``access_token_response`` compliance hook, so the
        return value is Authlib's next input: the answer is passed back
        untouched for the SDK to parse. Only the status line is logged, never
        the provider's body.
        """
        if response.status_code != 200:
            _log.warning(
                "auth.token_exchange.rejected",
                extra={
                    "event": "token_exchange_rejected",
                    "reason": "provider_status",
                    "status_code": response.status_code,
                },
            )
            raise LoginRejected("token_exchange_rejected") from None
        try:
            payload = response.json()
        except ValueError:
            raise LoginRejected("token_response_malformed") from None
        if not isinstance(payload, dict):
            raise LoginRejected("token_response_malformed")
        return response

    def _redeem(self, code: str, transaction: LoginTransaction) -> str:
        """POST the code to the provider's token endpoint and return the ID token.

        Authlib builds the form, performs the POST through the shared httpx
        client with Basic client authentication, and parses the answer; this
        method decides which answers are refusals.
        """
        endpoint = self._verifier.endpoint("token_endpoint")
        protocol = self._protocol_client()
        protocol.register_compliance_hook("access_token_response", self._check_answer)
        try:
            token = protocol.fetch_token(
                endpoint,
                grant_type="authorization_code",
                code=code,
                redirect_uri=transaction.redirect_uri,
                code_verifier=transaction.code_verifier,
                auth=(self._settings.client_id, self._settings.client_secret),
                headers={"accept": "application/json"},
            )
        except OAuth2Error:
            # A well-formed OAuth error: the provider's description is its
            # own wording about the code, and it stays with the provider.
            raise LoginRejected("token_exchange_rejected") from None
        except httpx.HTTPError:
            raise LoginRejected("token_endpoint_unreachable") from None

        # The gateway authenticates the browser session with the ID token. An
        # access token alone means the provider ignored response_type=code,
        # and accepting it would mean trusting a token whose audience is some
        # API rather than this client.
        id_token = token.get("id_token") if isinstance(token, dict) else None
        if not isinstance(id_token, str) or not id_token:
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
