"""Synthetic users for acceptance against the current Keycloak realm."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

from keycloak_admin import REALM, SYNTHETIC_PREFIX, LiveKeycloak, Secret, new_run_id


@dataclass(frozen=True)
class SyntheticUser:
    internal_id: str
    username: str
    password: Secret = field(repr=False)
    run_id: str = ""


def create_synthetic_user(live: LiveKeycloak, *, mfa: bool, account_id: str) -> SyntheticUser:
    runid = new_run_id()
    name = SYNTHETIC_PREFIX + "user-" + runid
    password = Secret(secrets.token_urlsafe(28))
    accountID = str(UUID(account_id))
    response = live.admin_request(
        "POST",
        "/admin/realms/" + REALM + "/users",
        json={
            "username": name,
            "enabled": True,
            "email": name + "@example.invalid",
            "emailVerified": True,
            "firstName": "Test",
            "lastName": "E03",
            "attributes": {
                "kb_account_id": [accountID],
                "c07.run_id": [runid],
                "c07.synthetic": ["true"],
            },
            "requiredActions": ["CONFIGURE_TOTP"] if mfa else [],
            "credentials": [
                {
                    "type": "password",
                    "value": password.reveal(),
                    "temporary": False,
                }
            ],
        },
    )
    if response.status_code != 201:
        raise RuntimeError("synthetic_user_create_failed")
    internal_id = str(UUID(urlsplit(response.headers["Location"]).path.rsplit("/", 1)[-1]))
    return SyntheticUser(internal_id, name, password, runid)


@contextmanager
def synthetic_user(
    live: LiveKeycloak,
    *,
    mfa: bool = False,
    account_id: str = "bbcbb06e-641d-401c-9a7b-ad7ac817d80f",
) -> Iterator[SyntheticUser]:
    user = create_synthetic_user(live, mfa=mfa, account_id=account_id)
    print(f"[c07-live] created user={user.internal_id} run_id={user.run_id}")
    try:
        yield user
    finally:
        path = f"/admin/realms/{REALM}/users/{user.internal_id}"
        logged_out = live.admin_request("POST", path + "/logout")
        if logged_out.status_code not in (204, 404):
            raise RuntimeError("synthetic_user_logout_failed")
        deleted = live.admin_request("DELETE", path)
        if deleted.status_code not in (204, 404):
            raise RuntimeError("synthetic_user_delete_failed")
        if live.admin_request("GET", path).status_code != 404:
            raise RuntimeError("synthetic_user_cleanup_failed")
        print(f"[c07-live] deleted user={user.internal_id} http={deleted.status_code}")
