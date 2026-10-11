"""Exercise actual Keycloak delivery to the deployed gateway."""

import time
from contextlib import ExitStack

import httpx
from keycloak_admin import REALM, LiveKeycloak
from keycloak_browser import GATEWAY, finish_login, password_step
from keycloak_user import synthetic_user


def check_provider_logout(live: LiveKeycloak) -> None:
    with synthetic_user(live) as user, synthetic_user(live) as unrelated, ExitStack() as stack:
        devices = [
            stack.enter_context(httpx.Client(timeout=20, follow_redirects=False)) for _ in range(2)
        ]
        other = stack.enter_context(httpx.Client(timeout=20, follow_redirects=False))
        for client in devices:
            finish_login(client, password_step(client, user), user)
        finish_login(other, password_step(other, unrelated), unrelated)
        response = live.admin_request(
            "POST", f"/admin/realms/{REALM}/users/{user.internal_id}/logout"
        )
        assert response.status_code == 204, "provider user logout failed"
        deadline = time.monotonic() + 10
        while True:
            states = [client.get(GATEWAY + "/auth/session") for client in devices]
            assert all(s.status_code == 200 for s in states), "session status lookup failed"
            if all(s.json().get("authenticated") is False for s in states):
                break
            assert time.monotonic() < deadline, "provider logout did not revoke both devices"
            time.sleep(0.2)
        preserved = other.get(GATEWAY + "/auth/session")
        assert preserved.status_code == 200, "unrelated session lookup failed"
        assert preserved.json().get("authenticated") is True, "unrelated user was logged out"
        assert preserved.json().get("principal_id") == unrelated.internal_id
