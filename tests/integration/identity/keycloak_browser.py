from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from keycloak_user import SyntheticUser

GATEWAY = "https://ai-wisdom.150-241-115-134.sslip.io"
IDP_HOST = "id.ai-wisdom.150-241-115-134.sslip.io"


def post_form(
    client: httpx.Client,
    page: httpx.Response,
    form_id: str,
    values: dict[str, str],
) -> httpx.Response:
    assert page.status_code == 200, "unexpected form page status"
    form = BeautifulSoup(page.text, "html.parser").find("form", id=form_id)
    form_found = form is not None
    assert form_found, "required login form is missing"
    action = urljoin(str(page.url), str(form.get("action", "")))
    parsed_action = urlsplit(action)
    valid_action = parsed_action.scheme == "https" and parsed_action.netloc == IDP_HOST
    assert valid_action, "unexpected form action"
    data = {
        str(field.get("name")): str(field.get("value", ""))
        for field in form.find_all("input", attrs={"type": "hidden"})
        if field.get("name") is not None
    }
    data.update(values)
    return client.post(action, data=data)


def password_step(client: httpx.Client, user: SyntheticUser) -> httpx.Response:
    redirect = client.get(f"{GATEWAY}/auth/login")
    assert redirect.status_code == 302, "unexpected login entry status"
    page = client.get(str(redirect.headers["location"]))
    assert page.status_code == 200, "unexpected login page status"
    return post_form(
        client,
        page,
        "kc-form-login",
        {"username": user.username, "password": user.password.reveal()},
    )


def finish_login(
    client: httpx.Client,
    response: httpx.Response,
    user: SyntheticUser,
) -> None:
    assert response.status_code == 302, "unexpected credentials response status"
    location = str(response.headers["location"])
    callback = urlsplit(location)
    gateway = urlsplit(GATEWAY)
    valid_callback = (
        callback.scheme == gateway.scheme
        and callback.netloc == gateway.netloc
        and callback.path == "/auth/callback"
    )
    assert valid_callback, "unexpected callback location"
    completed = client.get(location)
    assert completed.status_code == 302, "unexpected callback completion status"
    session = client.get(f"{GATEWAY}/auth/session")
    assert session.status_code == 200, "unexpected session response status"
    session_data = session.json()
    authenticated = session_data.get("authenticated")
    principal_id = session_data.get("principal_id")
    assert authenticated is True, "session is not authenticated"
    assert principal_id == user.internal_id, "unexpected session principal"
