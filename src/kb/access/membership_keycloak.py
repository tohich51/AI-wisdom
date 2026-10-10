"""C08 — the real Keycloak client for the second half of a deactivation.

READ THIS BEFORE TRUSTING IT
----------------------------

There is no Keycloak in this environment. No JVM, no container, no realm. Every
line below is therefore **unexecuted against a live service**, and the C08
result file records the check as ``not_run`` with the reason. It is not a mock,
it is not a stub, and it is not on any code path that a test exercises — it is
a real HTTP client against Keycloak's documented admin REST API, written now so
that the ordering guarantee in
:mod:`kb.access.membership_deactivation` has a real counterpart to call, and
labelled honestly so nobody counts it as verified.

What it does
------------

Keycloak 24+ exposes ``POST /admin/realms/{realm}/users/{user-id}/logout``,
which ends every user session of that account. The client obtains a token with
the ``client_credentials`` grant using a *service* account that holds
``realm-management``'s ``manage-users`` — the gateway never holds the root
provisioning secret, and this client is a separate identity from every human.

``subject`` is used as the user id. In Keycloak the ``sub`` claim of an access
token minted for a user in a realm is that user's id unless a custom protocol
mapper rewrites it, so this holds for a default configuration and is the first
thing to check when E03 finally runs against a real realm. The alternative —
resolving the user by username or email at logout time — needs a search
permission and a second round trip, and would be wrong for two people who share
an address.
"""

from __future__ import annotations

import logging
from typing import Any, Final

import httpx

from kb.access.membership_deactivation import RevocationOutcome

log = logging.getLogger(__name__)

#: Keycloak's own timeout vocabulary. A deactivation must not hang a request
#: thread for the provider's default, and it must not be so short that a busy
#: realm reports a member as not revoked.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0
#: A provider response body is never logged or journaled. A Keycloak error page
#: can echo a request id, a realm name and occasionally a client id, and the
#: journal is readable by an administrator and outlives the request.
_MAX_DETAIL_CHARS: Final[int] = 200


class KeycloakUnavailable(RuntimeError):
    """Keycloak could not be reached, or refused the service account.

    Raised rather than returned, because there is no sensible "partly revoked"
    state: the caller catches it and records an outstanding revocation.
    """


class KeycloakSessionRevoker:
    """Ends a user's provider sessions.

    Implements :class:`kb.access.membership_deactivation.SessionRevoker`. It does
    not touch PostgreSQL: the membership has already been closed and committed
    by the time anything here runs, and a provider client that could write to
    the authority table would make the ordering a suggestion.
    """

    def __init__(
        self,
        *,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        verify: bool | str = True,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not base_url or not realm or not client_id or not client_secret:
            # No defaults. A revocation client with a default endpoint or a
            # default realm is a client that revokes the wrong sessions.
            raise ValueError(
                "KeycloakSessionRevoker needs base_url, realm, client_id and client_secret"
            )
        self._base = base_url.rstrip("/")
        self._realm = realm
        self._client_id = client_id
        self._secret = client_secret
        self._verify = verify
        self._timeout = timeout

    def _token_url(self) -> str:
        return f"{self._base}/realms/{self._realm}/protocol/openid-connect/token"

    def _logout_url(self, subject: str) -> str:
        return f"{self._base}/admin/realms/{self._realm}/users/{subject}/logout"

    def _service_token(self, client: httpx.Client) -> str:
        response = client.post(
            self._token_url(),
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._secret,
            },
        )
        if response.status_code != 200:
            # Status only. The body of a failed token request contains the
            # realm's error description and must not reach a log line.
            raise KeycloakUnavailable(f"service token refused with {response.status_code}")
        payload: dict[str, Any] = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise KeycloakUnavailable("service token response carried no access_token")
        return token

    def revoke_sessions(self, *, issuer: str, subject: str) -> RevocationOutcome:
        """Log the user out everywhere in the realm.

        ``issuer`` is accepted because the protocol requires it and because it is
        the thing that would have to be checked if this ever served more than
        one realm: a subject from another issuer is not this realm's user, and
        logging out a stranger's id is a denial of service against another
        tenant. With one realm per deployment that cannot happen, and the check
        that would catch it belongs to the deployment that has two.
        """
        if not subject:
            return RevocationOutcome(revoked=False, reason="no_subject")
        try:
            with httpx.Client(
                verify=self._verify, timeout=self._timeout, follow_redirects=False
            ) as client:
                token = self._service_token(client)
                response = client.post(
                    self._logout_url(subject),
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            # Type name only, never the URL with its query string.
            log.warning("keycloak unreachable: %s", type(exc).__name__)
            return RevocationOutcome(revoked=False, reason="provider_unreachable")

        if response.status_code in (204, 200):
            return RevocationOutcome(
                revoked=True,
                reason="logged_out",
                detail={"status": response.status_code, "issuer_host": _host(issuer)},
            )
        # 404 means "no such user in this realm", which is not the same as "the
        # sessions are gone" — it may mean the id belongs to another realm. It
        # is reported as outstanding, not as success.
        return RevocationOutcome(
            revoked=False,
            reason=f"provider_refused_{response.status_code}"[:_MAX_DETAIL_CHARS],
            detail={"status": response.status_code},
        )


def _host(issuer: str) -> str:
    """The issuer's host, for the journal.

    The host, never the whole issuer: an issuer URL is an identity coordinate,
    and a policy journal is not the place to accumulate them.
    """
    from urllib.parse import urlsplit

    return (urlsplit(issuer).hostname or "")[:120]
