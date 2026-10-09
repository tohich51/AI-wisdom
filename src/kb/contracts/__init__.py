"""Single source of schema for the API, the MCP surface and the export
generator. Import from here rather than from the submodules."""

from kb.contracts import entities, enums, mcp_tools
from kb.contracts.mcp_tools import (
    EXPECTED_TOOLS,
    FORBIDDEN_ARG_FIELDS,
    FORBIDDEN_MCP_TOOLS,
    MCP_TOOLS,
    assert_no_identity_injection,
)

__all__ = [
    "EXPECTED_TOOLS",
    "FORBIDDEN_ARG_FIELDS",
    "FORBIDDEN_MCP_TOOLS",
    "MCP_TOOLS",
    "assert_no_identity_injection",
    "entities",
    "enums",
    "mcp_tools",
]
