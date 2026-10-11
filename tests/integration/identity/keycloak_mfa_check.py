import base64
import time
from urllib.parse import urlsplit

import httpx
import pyotp
from bs4 import BeautifulSoup
from keycloak_admin import REALM, LiveKeycloak, Secret
from keycloak_browser import GATEWAY, IDP_HOST, finish_login, password_step, post_form
from keycloak_user import synthetic_user


def check_mfa(live: LiveKeycloak) -> None:
    with synthetic_user(live, mfa=True) as user:
        with httpx.Client(base_url=GATEWAY, timeout=20, follow_redirects=False) as client:
            page = password_step(client, user)
            # Keycloak redirects the password POST to its required-action page.
            for _ in range(4):
                location = page.headers.get("location", "")
                target = urlsplit(location)
                if page.status_code != 302 or target.netloc != IDP_HOST:
                    break
                assert target.scheme == "https", "unsafe required-action redirect"
                page = client.get(location)
            assert page.status_code == 200, "password step did not return the MFA setup page"
            setup = BeautifulSoup(page.text, "html.parser").find("form", id="kc-totp-settings-form")
            assert setup is not None, "MFA setup form is missing"
            seed_input = setup.find("input", attrs={"name": "totpSecret"})
            assert seed_input is not None and seed_input.get("value"), "TOTP seed is missing"
            # Keycloak's hidden field is raw; its authenticator key is Base32.
            seed = Secret(base64.b32encode(str(seed_input["value"]).encode()).decode("ascii"))
            generator = pyotp.TOTP(seed.reveal())
            page = post_form(
                client,
                page,
                "kc-totp-settings-form",
                {"totp": generator.now(), "userLabel": "test-e03"},
            )
            assert page.status_code == 302, "MFA enrollment did not redirect"
            finish_login(client, page, user)

        response = live.admin_request(
            "GET", f"/admin/realms/{REALM}/users/{user.internal_id}/credentials"
        )
        assert response.status_code == 200, "credential lookup failed"
        credentials = response.json()
        assert any(item.get("type") == "otp" for item in credentials), "OTP credential is missing"

        with httpx.Client(base_url=GATEWAY, timeout=20, follow_redirects=False) as client:
            page = password_step(client, user)
            assert page.status_code == 200, "password step did not return the OTP challenge"
            forms = [
                form
                for form in BeautifulSoup(page.text, "html.parser").find_all("form")
                if form.find("input", attrs={"name": "otp"}) is not None
            ]
            assert forms, "OTP challenge form is missing"
            otp_form_id = forms[0].get("id")
            assert isinstance(otp_form_id, str) and otp_form_id, "OTP form identifier is missing"

            now = int(time.time())
            nearby_codes = {generator.at(now + offset) for offset in (-30, 0, 30)}
            wrong_code = f"{(int(generator.at(now)) + 1) % 1_000_000:06d}"
            while wrong_code in nearby_codes:
                wrong_code = f"{(int(wrong_code) + 1) % 1_000_000:06d}"
            page = post_form(client, page, otp_form_id, {"otp": wrong_code})
            assert page.status_code == 200, "wrong OTP unexpectedly redirected"
            challenge_forms = BeautifulSoup(page.text, "html.parser").find_all("form")
            assert any(
                form.get("id") == otp_form_id
                and form.find("input", attrs={"name": "otp"}) is not None
                for form in challenge_forms
            ), "wrong OTP did not preserve the challenge"

            time.sleep(30 - (time.time() % 30) + 1)
            page = post_form(client, page, otp_form_id, {"otp": generator.now()})
            assert page.status_code == 302, "correct OTP did not redirect"
            finish_login(client, page, user)
