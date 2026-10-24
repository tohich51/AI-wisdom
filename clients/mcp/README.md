# AI-wisdom optional experimental MCP client

This directory is an OPTIONAL client, not an application requirement or a live activation. It bridges a long-lived SDK connection to a private Unix socket and exposes stdio MCP to a desktop/agent client. No Codex CLI, owner configuration, grants, books or result cache is included or needed by the SDK path.

## Supported environment

Use CPython 3.12 (locally checked: 3.12.14 on macOS arm64). The application-compatible range is >=3.11,<3.13; 3.11 is not exercised in this handoff. macOS and Linux/Unix with AF_UNIX, fcntl, POSIX permissions and signal handlers are the intended scope; Windows is not supported. Linux execution is not verified here.

From this directory, create a dedicated environment (do not install into the main app or global Python):

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python ai_wisdom_mcp.py --help
```

The exact MCP SDK is mcp==2.3.0. Its transport is httpx2==2.13.1, NOT httpx. Pydantic and AnyIO are explicit runtime dependencies; the SDK installs its remaining dependencies, including mcp-types==2.3.0. `checks/test-environment.txt` records the successfully exercised dependency set; it is evidence, not a copied virtual environment or a claim about every future resolver result. Use a Python with SSL and lzma support. All application modules are next to the launcher; no external PYTHONPATH is needed.

## Configure your own server and OAuth client

Get the HTTPS MCP URL, exact expected OAuth issuer (including its realm path), and public PKCE client ID from your server administrator. The configured client must allow authorization_code and refresh_token, token_endpoint_auth_method=none, and the redirect:

    http://127.0.0.1:19289/callback/sdk-backend

The default public client ID is `kb-mcp`. Override it with `--sdk-client-id` if your deployment uses another registration. No automatic admin client creation is performed. Default scopes are exactly:

    openid offline_access kb:read kb:contribute kb:experience kb:export

`--sdk-scope` accepts your deployment's approved scope string; it does not grant access or request administrator permissions. The daemon targets an AI-wisdom-compatible tool server: its idle heartbeat uses `list_libraries`. Generic configuration does not promise interoperability with every unrelated MCP server.

Example foreground daemon (replace the two example HTTPS URLs):

```sh
umask 077
STATE="$HOME/.local/state/ai-wisdom-mcp"
mkdir -p "$STATE"
chmod 700 "$STATE"
.venv/bin/python ai_wisdom_mcp.py daemon \
  --socket "$STATE/broker.sock" \
  --sdk-state-dir "$STATE/client" \
  --sdk-url https://knowledge.example/mcp \
  --sdk-issuer https://identity.example/realms/knowledge-hub
```

SDK is the default backend. URL, issuer, state directory and socket are explicit: there is no owner endpoint or private-machine default. Keep the Unix socket path short enough for your OS's AF_UNIX limit. The socket parent must be owned by you with mode 0700; the socket is 0600. Use one daemon and one state directory per configuration. State files are plaintext sensitive OAuth material protected by filesystem permissions, not encrypted storage. Keep them out of repositories, backups shared with others, and public attachments.

On first startup, normal SDK discovery performs `tools/list`, which initiates OAuth when required. A local browser is opened using `open` on macOS or `xdg-open` on Linux. If opening fails, stderr identifies a private `authorize-url.txt`; open its URL yourself on this same device. Do not paste that URL into chat. Complete the browser flow promptly (the inherited backend startup deadline is 180 seconds). The loopback listener verifies the path; the SDK verifies PKCE state and authorization-response issuer. No device code, copied grant or token import is needed.

After successful authorization, this client's own tokens, expiry and validated metadata are saved in the private `client/oauth/` directory. Restarts restore expiry and refresh through the validated issuer endpoint. Refresh can run unattended while the grant remains valid. Revocation, expired refresh grants, changed consent or server policy can require browser OAuth again; unattended does not mean bypassing consent. Do not reuse this directory with a different resource, issuer, client ID, scope or redirect: it now fails closed on configuration mismatch, including older unbound state. Create a separate private directory and authorize normally instead.

For a second computer, install the package there, use a separate private state directory, and perform a separate normal browser authorization for that device. Do not copy grants, authorization URLs, owner credential stores or customer data between devices.

## Stdio MCP configuration

Start the daemon above first and wait for its stderr `status: ready`. Then configure your MCP consumer with absolute paths for YOUR installation and socket. JSON configuration does not expand `$HOME` or `~`:

```json
{
  "mcpServers": {
    "ai-wisdom": {
      "command": "/absolute/client/.venv/bin/python",
      "args": [
        "/absolute/client/ai_wisdom_mcp.py",
        "stdio", "--socket", "/absolute/private-state/broker.sock"
      ]
    }
  }
}
```

The stdio process connects to that daemon; it does not start it or initiate another OAuth grant. Multiple stdio sessions share the daemon's serialized tool queue. No service manager is installed. Stop the daemon with SIGINT/SIGTERM. The broker does not automatically replay unknown tool outcomes or switch to another backend. Use application-level idempotency and inspect the outcome before retrying writes.

## Catalog and compatibility

Normal portable setup omits `--sdk-tool-catalog` and discovers the actual server's tools through the SDK. Connection modes remain `2026-07-28`, then SDK `auto` on connect-only failure. A locally ready session never reconnects to replay a tool call.

`tool_catalog.json` is an explicit OPTIONAL SAMPLE snapshot of 35 tools from accepted server commit `66a174cc6b4fb31ff30d99e7cfb799ab28279271`. Only its owner-specific `mcp_url` metadata was replaced with `https://ai-wisdom.example/mcp`; all tool metadata is unchanged. The sample is not evidence about a fresh server and its example domain is not a production service.

The sample checksum pin is `33a52dfca53820eba540774fe33b94cdc8e0d6266b1e7fbbfadb65380e36ed28`. Snapshot mode (`--sdk-tool-catalog /path/to/tool_catalog.json`) requires the exact checksum, exact URL, release identity, 35 unique valid tools and compatible SDK metadata absorption. It cannot be enabled for a different URL by editing JSON: that fails checksum validation. A release maintainer must independently validate a deployment-specific artifact and update the artifact and source pin together. Do not remove checksum checks. Snapshot readiness is only local metadata readiness, not authentication, live tool compatibility, RLS or ACL proof. Foreground calls still require OAuth and current server authorization.

## Experimental refresh overlap

`--sdk-refresh-overlap` is OFF by default. When explicitly selected, the existing implementation can prepare the same HTTPS origin with one unauthenticated GET `/healthz` during a refresh based on restored, validated metadata. The raw request carries no auth, cookies, private headers or parameters, follows no redirects, and its response is discarded. It uses the same client pool, a two-second preparation deadline and bounded cleanup. The origin still sees this request and ordinary connection metadata. It is not a protected-call replay, does not warm a tool, and does not prove authorization. No preparation is sent for initial discovery or a still-valid token. Failure does not become a tool retry.

No import-overlap optimization or new latency optimization is included. The prior <=2-second performance goal remains UNMET. No new live latency measurement was performed; synthetic test durations are not network performance measurements. Remote latency, cold OAuth and tool execution may exceed that target.

## Legacy native adapter

The `--backend native`, `--binary` and `--cwd` selector interface is retained, but the owner-bound legacy adapter and native helper are deliberately not distributed. Selecting it in this package fails explicitly, without SDK fallback. They remain unchanged in the frozen review input; SDK operation never imports or executes them. A future separately reviewed portable native adapter is outside this package's scope.

## Offline checks

```sh
mkdir -p .tmp
export TMPDIR="$PWD/.tmp"
export PYTHONDONTWRITEBYTECODE=1
.venv/bin/python tests/test_portable.py
.venv/bin/python tests/test_refresh_overlap.py
.venv/bin/python tests/test_sdk_backend.py
```

The retained SDK test uses synthetic loopback callback fixtures only, on 127.0.0.1 ports 19391/19392. All other HTTP is mocked with the real SDK/httpx2 transport; no live OAuth, external network, model calls or products are used. Run with those ports free. Tests exercise configuration binding, discovery/snapshot behavior, metadata fail-closed boundaries, scope/PKCE, same-task lifetime, no replay, graph wire integrity and opt-in cleanup. They do not certify a live server, current grants, RLS or citations from customer data. Root owns integration/acceptance; the changed OAuth configuration seam requires independent security review before release.
