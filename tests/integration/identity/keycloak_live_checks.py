import httpx
from keycloak_admin import LiveKeycloak
from keycloak_browser import GATEWAY, finish_login, password_step
from keycloak_user import synthetic_user


def check_interactive(live: LiveKeycloak) -> None:
    with synthetic_user(live) as user:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            finish_login(client, password_step(client, user), user)
            response = client.get(GATEWAY + "/api/libraries")
            assert response.status_code == 403, "non-member library access was not denied"
