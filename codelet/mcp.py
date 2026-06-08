"""MCP (Model Context Protocol) stub for codelet.

Mirrors the reference agent's MCP support:
- MCP client connections
- MCP tool registration
"""

from __future__ import annotations

from typing import Any, Dict, List


class MCPClient:
    """Stub MCP client."""

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.tools: List[dict] = []

    def list_tools(self) -> List[dict]:
        """Return tools exposed by this MCP server."""
        return self.tools

    def add_tool(self, name: str, description: str, schema: dict) -> None:
        self.tools.append({
            "name": f"mcp_{self.name}_{name}",
            "description": description,
            "schema": schema,
        })


def connect_mcp_server(name: str, config: dict) -> MCPClient:
    """Connect to an MCP server and return a client handle."""
    client = MCPClient(name, config)
    # Stub: auto-add a dummy tool for testing
    client.add_tool("dummy", "A dummy MCP tool", {"query": "str"})
    return client
