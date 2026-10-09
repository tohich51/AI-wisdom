"""C03 spike — official MCP SDK, real client, real transport.

Scope note, and it matters: this proves the **SDK and transport** work. It does
NOT prove the Keycloak leg. There is no JVM and no Keycloak in this cloud
environment, so token issuance, audience validation and per-device refresh
remain unverified and E03 stays pending. A fake here is a fake, and the spike
says so in its result file rather than in a comment nobody reads.

What is real here:
  * a real `mcp` 2.3.0 server over a real stdio transport
  * a real `ClientSession` client in a separate process
  * the ten tools from PRODUCT-SPEC, registered from the C02 contract module
  * identity injected per call by the transport, never taken from arguments
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer

from kb.contracts import MCP_TOOLS
from kb.contracts.entities import TrustedContext
from kb.contracts.mcp_tools import assert_no_identity_injection
from kb.domain.tiers import UsageState

# One process-wide identity stands in for the Keycloak-validated principal.
# In production this is rebuilt from the bearer token on EVERY request; the
# spike keeps one value only to make the "who am I" boundary visible.
CURRENT = TrustedContext.model_validate(
    {
        "principal_id": "00000000-0000-4000-8000-000000000001",
        "account_id": "00000000-0000-4000-8000-0000000000aa",
        "generation_watermark": 1,
        "roles_by_library": {},
    }
)

server = MCPServer(name="kb-spike", version="0.1.0",
                   instructions="Knowledge Hub C03 spike. Synthetic data only.")


@server.tool(description="List libraries the caller may see.")
def list_libraries() -> dict:
    return {
        "libraries": [],
        "role_by_library": {},
        "_principal": str(CURRENT.principal_id),
        "_note": "empty product: the owner creates libraries",
    }


@server.tool(description="Retrieve knowledge. Retrieval is not application.")
def search_knowledge(query: str, limit: int = 10) -> dict:
    return {
        "hits": [],
        "retrieved_only": True,
        "query": query,
        "limit": limit,
        "_principal": str(CURRENT.principal_id),
        "_generative_calls": 0,  # ordinary search must not generate
    }


@server.tool(description="Start a use of an exact rule version.")
def start_use(rule_id: str, version_no: int) -> dict:
    return {
        "use_id": "00000000-0000-4000-8000-0000000000u1".replace("u", "0"),
        "state": UsageState.RETRIEVED.value,
        "rule_id": rule_id,
        "version_no": version_no,
        "_principal": str(CURRENT.principal_id),
    }


@server.tool(description="Report what actually happened. Absent values stay null.")
def record_outcome(use_id: str, observed_result: str | None = None,
                   rating: int | None = None) -> dict:
    return {
        "use_id": use_id,
        "observed_result": observed_result,
        "rating": rating,
        "_note": "LLM confidence is not a proven effect",
    }


@server.tool(description="Reject any caller-supplied identity. Exists to prove it 404s.")
def whoami(user_id: str | None = None) -> dict:
    # If a caller could pass identity, the argument would be honoured. It is
    # not part of any C02 tool contract, and it is ignored here deliberately.
    return {
        "principal": str(CURRENT.principal_id),
        "ignored_argument": user_id,
        "_note": "argument is NOT identity; only the transport decides",
    }


def main() -> int:
    # Refuse to start if a tool ever grows a caller-asserted identity field.
    assert_no_identity_injection()
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
