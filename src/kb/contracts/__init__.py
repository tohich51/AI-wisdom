"""Single source of schema for the API, the MCP surface and the export
generator. Import from here rather than from the submodules."""

from kb.contracts import entities, enums, mcp_tools, operations
from kb.contracts.mcp_tools import (
    EXPECTED_TOOLS,
    FORBIDDEN_ARG_FIELDS,
    FORBIDDEN_MCP_TOOLS,
    MCP_TOOLS,
    assert_no_identity_injection,
)
from kb.contracts.operations import (
    ALIAS_PREFIX,
    API_PREFIXES,
    CANONICAL_PREFIX,
    CONTRACT_REVISION,
    MCP_OPERATIONS,
    OPERATIONS,
    Effect,
    Operation,
    Page,
    assert_registry_consistent,
    canonical_api_path,
    operation_for,
)

__all__ = [
    "ALIAS_PREFIX",
    "API_PREFIXES",
    "CANONICAL_PREFIX",
    "CONTRACT_REVISION",
    "EXPECTED_TOOLS",
    "FORBIDDEN_ARG_FIELDS",
    "FORBIDDEN_MCP_TOOLS",
    "MCP_OPERATIONS",
    "MCP_TOOLS",
    "OPERATIONS",
    "Effect",
    "Operation",
    "Page",
    "assert_no_identity_injection",
    "assert_registry_consistent",
    "canonical_api_path",
    "entities",
    "enums",
    "mcp_tools",
    "operation_for",
    "operations",
]
