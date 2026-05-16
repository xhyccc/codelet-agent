"""Optional protocol integrations for mini-coding-agent.

Each submodule is independently importable and has no required runtime
dependencies beyond the Python standard library:

* :mod:`mini_coding_agent.protocols.mcp` - Minimal MCP (Model Context
  Protocol) stdio client + server.  Lets the agent consume external tools
  exposed by MCP servers (``mcp__<server>__<tool>``), and lets external
  hosts consume the agent's own tools.
* :mod:`mini_coding_agent.protocols.a2a` - Tiny A2A (Agent-to-Agent) HTTP
  server: publishes the agent card at ``/.well-known/agent.json`` and
  accepts ``tasks/send`` JSON-RPC requests.
* :mod:`mini_coding_agent.protocols.acp` - ACP scaffolding stub.  Defines
  the message shape so callers can experiment, but the full protocol is
  not bundled.

These modules are intentionally tiny and dependency-free so the core
agent stays lean.  Production deployments are encouraged to swap each
file for the upstream reference implementation when stability matters.
"""

from .acp import ACPSessionStub  # noqa: F401
from .a2a import (  # noqa: F401
    A2AAgentCard,
    build_agent_card,
    make_a2a_app,
    serve_a2a_blocking,
)
from .mcp import (  # noqa: F401
    MCPClient,
    MCPClientError,
    MCPServerHandle,
    discover_mcp_servers,
    load_mcp_config,
    register_mcp_tools,
    serve_mcp_stdio,
)
