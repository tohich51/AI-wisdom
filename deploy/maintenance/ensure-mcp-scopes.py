"""Idempotently configure optional action scopes on the existing Keycloak MCP client.

Uses the existing private administrator environment file; never prints credentials.
No account, password, redirect, role or library grant is changed.
"""

import argparse
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCOPES = ("kb:read", "kb:contribute", "kb:experience", "kb:export")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--client", choices=["kb-mcp", "kb-minimax-cloud"], default="kb-mcp")
    parser.add_argument("--base-url", default="http://127.0.0.1:19281")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.base_url != "http://127.0.0.1:19281":
        parser.error("only the existing loopback Keycloak endpoint is permitted")
    env = dict(
        line.split("=", 1)
        for line in (args.config_dir / "keycloak.env").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )

    def request(path, method="GET", data=None, token=None, form=False):
        headers = {"Authorization": "Bearer " + token} if token else {}
        if data is not None:
            headers["Content-Type"] = (
                "application/x-www-form-urlencoded" if form else "application/json"
            )
        payload = (
            (urlencode(data) if form else json.dumps(data)).encode() if data is not None else None
        )
        req = Request(  # noqa: S310 - verified fixed loopback HTTP URL
            args.base_url.rstrip("/") + path, data=payload, method=method, headers=headers
        )
        with urlopen(req, timeout=30) as response:  # noqa: S310 - operator's fixed Keycloak base URL
            body = response.read()
            return json.loads(body) if body else None

    token = request(
        "/realms/master/protocol/openid-connect/token",
        "POST",
        {
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": env["KC_BOOTSTRAP_ADMIN_USERNAME"],
            "password": env["KC_BOOTSTRAP_ADMIN_PASSWORD"],
        },
        form=True,
    )["access_token"]
    prefix = "/admin/realms/knowledge-hub"
    clients = request(prefix + "/clients?clientId=" + args.client, token=token)
    if len(clients) != 1:
        raise RuntimeError("existing_mcp_client_required")
    client_id = clients[0]["id"]
    all_scopes = {s["name"]: s for s in request(prefix + "/client-scopes", token=token)}
    for name in SCOPES:
        if name not in all_scopes:
            if not args.apply:
                continue
            request(
                prefix + "/client-scopes",
                "POST",
                {
                    "name": name,
                    "protocol": "openid-connect",
                    "attributes": {
                        "include.in.token.scope": "true",
                        "display.on.consent.screen": "true",
                    },
                },
                token,
            )
    all_scopes = {s["name"]: s for s in request(prefix + "/client-scopes", token=token)}
    if args.apply:
        for name in SCOPES:
            scope = all_scopes[name]
            if scope["protocol"] != "openid-connect":
                raise RuntimeError("scope_protocol_mismatch")
            if scope.get("attributes", {}).get("include.in.token.scope") != "true":
                scope["attributes"] = {
                    **scope.get("attributes", {}),
                    "include.in.token.scope": "true",
                }
                request(prefix + f"/client-scopes/{scope['id']}", "PUT", scope, token)
            request(
                prefix + f"/clients/{client_id}/optional-client-scopes/{scope['id']}",
                "PUT",
                token=token,
            )
    optional = {
        s["name"]
        for s in request(prefix + f"/clients/{client_id}/optional-client-scopes", token=token)
    }
    missing = sorted(set(SCOPES) - optional)
    configured = {s["name"]: s for s in request(prefix + "/client-scopes", token=token)}
    if any(
        configured.get(name, {}).get("attributes", {}).get("include.in.token.scope") != "true"
        for name in SCOPES
    ):
        raise RuntimeError("action_scope_not_included_in_tokens")
    print(
        json.dumps(
            {
                "client_id": args.client,
                "optional_scopes": list(SCOPES),
                "missing": missing,
                "credential_changes": False,
                "grant_changes": False,
            }
        )
    )
    if missing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
