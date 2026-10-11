import httpx
from keycloak_admin import PRODUCT_CLIENT, REALM, LiveKeycloak, Secret
from keycloak_browser import finish_login, password_step
from keycloak_user import synthetic_user


def check_password_grant(live: LiveKeycloak) -> None:
    with synthetic_user(live) as user, httpx.Client(timeout=20, follow_redirects=False) as client:
        finish_login(client, password_step(client, user), user)

        product_client_ids = [
            item["id"] for item in live.clients() if item["clientId"] == PRODUCT_CLIENT
        ]
        assert len(product_client_ids) == 1
        client_id = product_client_ids[0]

        secret_response = live.admin_request(
            "GET",
            f"/admin/realms/{REALM}/clients/{client_id}/client-secret",
        )
        assert secret_response.status_code == 200
        secret = Secret(secret_response.json()["value"])

        token_response = client.post(
            live.discovery(public=True)["token_endpoint"],
            data={
                "grant_type": "password",
                "client_id": PRODUCT_CLIENT,
                "client_secret": secret.reveal(),
                "username": user.username,
                "password": user.password.reveal(),
            },
        )
        assert token_response.status_code == 400
        assert token_response.json().get("error") == "unauthorized_client"
