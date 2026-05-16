"""Model Context Protocol (MCP) client + server.

Uses the official ``mcp`` PyPI package for protocol handling.

* :class:`MCPClient` - synchronous wrapper around the async
  ``mcp.ClientSession``.  Launches a server subprocess, drives the
  ``initialize`` handshake, lists tools, and proxies tool calls.

Wire format: line-delimited JSON-RPC 2.0 over stdio (stdin/stdout).

Config: by default we look for ``.mini-coding-agent/mcp.json`` in the
workspace root.  Schema::

    {
      "servers": {
        "fs": {"command": ["python", "-m", "some_mcp_fs_server"]},
        "weather": {"command": ["./weather_server"], "env": {"API_KEY": "..."}}
      }
    }

Each declared server is launched as a subprocess; its tools are surfaced
to the agent as ``mcp__<server>__<tool>`` so namespaces never collide.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPClientError(RuntimeError):
    """Raised on MCP protocol or transport failures."""


# ---------------------------------------------------------------------------
# Client  (synchronous façade over the official async mcp.ClientSession)
# ---------------------------------------------------------------------------


class MCPClient:
    """Synchronous MCP client backed by the official ``mcp`` SDK.

    The client launches the server as a subprocess and drives the
    ``initialize`` handshake via the official :class:`mcp.ClientSession`.
    An asyncio event-loop runs in a background daemon thread; all public
    methods are synchronous and submit coroutines to that loop via
    :func:`asyncio.run_coroutine_threadsafe`.
    """

    def __init__(self, name: str, command: List[str], env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None, timeout: float = 10.0):
        self.name = name
        self._command = command[0] if command else ""
        self._args = list(command[1:]) if len(command) > 1 else []
        self._env = env
        self._cwd = str(cwd) if cwd else None
        self.timeout = timeout
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[ClientSession] = None
        self._stdio_exit_fn: Any = None
        self._session_exit_fn: Any = None

    # ---- lifecycle ------------------------------------------------------

    def _run_loop(self) -> None:
        assert self._loop is not None
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_start(self) -> None:
        server_params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
            cwd=self._cwd,
        )
        stdio_cm = stdio_client(server_params)
        read, write = await stdio_cm.__aenter__()
        self._stdio_exit_fn = stdio_cm.__aexit__
        session_cm = ClientSession(read, write)
        self._session = await session_cm.__aenter__()
        self._session_exit_fn = session_cm.__aexit__
        await self._session.initialize()

    async def _async_stop(self) -> None:
        for fn in (self._session_exit_fn, self._stdio_exit_fn):
            if fn is not None:
                try:
                    await fn(None, None, None)
                except Exception:
                    pass
        self._session_exit_fn = None
        self._stdio_exit_fn = None
        self._session = None

    def start(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        future = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
        try:
            future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError as exc:
            self._loop.call_soon_threadsafe(self._loop.stop)
            raise MCPClientError("timeout waiting for initialize") from exc
        except Exception as exc:
            self._loop.call_soon_threadsafe(self._loop.stop)
            raise MCPClientError(str(exc)) from exc

    def stop(self) -> None:
        if self._loop is None:
            return
        loop = self._loop
        self._loop = None
        if self._session is not None:
            future = asyncio.run_coroutine_threadsafe(self._async_stop(), loop)
            try:
                future.result(timeout=2.0)
            except Exception:
                pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    # ---- internal helpers ----------------------------------------------

    def _submit(self, coro: Any) -> Any:
        """Submit *coro* to the background event loop and block for its result."""
        if self._loop is None or self._session is None:
            raise MCPClientError("server not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError as exc:
            raise MCPClientError("timeout") from exc
        except Exception as exc:
            raise MCPClientError(str(exc)) from exc

    # ---- high-level API ------------------------------------------------

    def list_tools(self) -> List[dict]:
        """Return the server's tool list as plain dicts."""
        result: mcp_types.ListToolsResult = self._submit(self._session.list_tools())
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema if isinstance(t.inputSchema, dict) else {},
            }
            for t in result.tools
        ]

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        """Call a tool and return the raw MCP result as a plain dict."""
        result: mcp_types.CallToolResult = self._submit(
            self._session.call_tool(name, arguments or {})
        )
        return {
            "content": [
                {"type": c.type, "text": getattr(c, "text", "")}
                for c in result.content
            ]
        }


# ---------------------------------------------------------------------------
# Config discovery
# ---------------------------------------------------------------------------


def load_mcp_config(path: Path) -> dict:
    """Load and validate an MCP config JSON file.  Returns ``{}`` if missing."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MCPClientError(f"mcp config invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MCPClientError("mcp config must be a JSON object")
    servers = data.get("servers") or {}
    if not isinstance(servers, dict):
        raise MCPClientError("mcp config 'servers' must be an object")
    for name, spec in servers.items():
        if not isinstance(spec, dict) or "command" not in spec:
            raise MCPClientError(f"mcp server '{name}' missing 'command'")
        if not isinstance(spec["command"], list):
            raise MCPClientError(f"mcp server '{name}' command must be a list")
    return data


def discover_mcp_servers(workspace_root: Path) -> dict:
    """Locate an ``mcp.json`` file in the workspace (or user-home fallback)."""
    candidates = [
        workspace_root / ".mini-coding-agent" / "mcp.json",
        workspace_root / "mcp.json",
        Path.home() / ".mini-coding-agent" / "mcp.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return load_mcp_config(candidate)
    return {}


def register_mcp_tools(agent, config: dict) -> List[MCPClient]:
    """For each declared MCP server, start a client and add its tools
    to ``agent.tools`` under the ``mcp__<server>__<tool>`` prefix.

    Returns the list of started clients so the caller can shut them down
    on exit.  Failures to start an individual server are surfaced as
    warnings on stderr but do not abort registration.
    """
    import sys as _sys

    started: List[MCPClient] = []
    servers = (config or {}).get("servers") or {}
    for server_name, spec in servers.items():
        client = MCPClient(
            name=server_name,
            command=spec["command"],
            env=spec.get("env"),
            cwd=spec.get("cwd"),
            timeout=float(spec.get("timeout", 10.0)),
        )
        try:
            client.start()
            tools = client.list_tools()
        except Exception as exc:  # noqa: BLE001 - degraded mode is okay
            print(f"[mcp] failed to start server '{server_name}': {exc}",
                  file=_sys.stderr)
            client.stop()
            continue
        started.append(client)
        for tool in tools:
            tool_name = tool.get("name") if isinstance(tool, dict) else None
            if not tool_name:
                continue
            full_name = f"mcp__{server_name}__{tool_name}"
            description = (tool.get("description") if isinstance(tool, dict) else "") or full_name
            schema = (tool.get("inputSchema") if isinstance(tool, dict) else {}) or {}

            def _run(args, _client=client, _tool=tool_name):
                payload = _client.call_tool(_tool, args or {})
                # MCP tool results are usually {"content": [{"type":"text","text":...}, ...]}
                if isinstance(payload, dict):
                    parts = payload.get("content") or []
                    text_parts = [
                        p.get("text", "") for p in parts
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    if text_parts:
                        return "\n".join(text_parts)
                    return json.dumps(payload, ensure_ascii=False)
                return str(payload)

            agent.tools[full_name] = {
                "schema": {"__mcp_schema__": json.dumps(schema)[:200]},
                "risky": False,
                "description": f"[mcp:{server_name}] {description}",
                "run": _run,
            }
    return started


# ---------------------------------------------------------------------------
# Server (expose own tools as MCP over stdio)
# ---------------------------------------------------------------------------


@dataclass
class MCPServerHandle:
    """Handle returned by :func:`serve_mcp_stdio` for graceful shutdown."""

    thread: threading.Thread
    stop_event: threading.Event

    def stop(self):
        self.stop_event.set()


def serve_mcp_stdio(agent, *, stdin=None, stdout=None, blocking: bool = True) -> Optional[MCPServerHandle]:
    """Expose ``agent.tools`` as an MCP server over stdio.

    Uses the official ``mcp`` package types (:mod:`mcp.types`) for all
    protocol message construction.  The transport layer is a simple
    line-delimited JSON loop so that callers can inject custom
    ``stdin``/``stdout`` objects (e.g. :class:`io.StringIO`) for testing.

    When ``blocking=True`` the call returns only when stdin closes.  When
    ``blocking=False`` a background thread is started and a handle is
    returned for graceful shutdown.  ``stdin`` / ``stdout`` default to
    ``sys.stdin`` / ``sys.stdout``.
    """
    import sys as _sys

    in_stream = stdin or _sys.stdin
    out_stream = stdout or _sys.stdout
    stop_event = threading.Event()

    def _handle(msg: dict) -> Optional[dict]:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            result = mcp_types.InitializeResult(
                protocolVersion=mcp_types.LATEST_PROTOCOL_VERSION,
                capabilities=mcp_types.ServerCapabilities(
                    tools=mcp_types.ToolsCapability(),
                ),
                serverInfo=mcp_types.Implementation(name="mini-coding-agent", version="0.x"),
            )
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": result.model_dump(by_alias=True, exclude_none=True),
            }
        if method == "notifications/initialized":
            return None  # notification – no response
        if method == "tools/list":
            tools = [
                mcp_types.Tool(
                    name=name,
                    description=spec.get("description", ""),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            k: {"type": "string", "description": str(v)}
                            for k, v in (spec.get("schema") or {}).items()
                        },
                    },
                )
                for name, spec in agent.tools.items()
            ]
            result = mcp_types.ListToolsResult(tools=tools)
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": result.model_dump(by_alias=True, exclude_none=True),
            }
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                tool_result = agent.run_tool(tool_name, arguments)
            except Exception as exc:  # noqa: BLE001 - surface as error
                return {
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            call_result = mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type="text", text=str(tool_result))],
            )
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": call_result.model_dump(by_alias=True, exclude_none=True),
            }
        # Unknown method
        return {
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    def _serve():
        while not stop_event.is_set():
            line = in_stream.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                # Validate incoming message using official mcp types.
                mcp_types.JSONRPCMessage.model_validate_json(line)
                msg = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            response = _handle(msg)
            if response is not None:
                out_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
                out_stream.flush()

    if blocking:
        _serve()
        return None

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return MCPServerHandle(thread=thread, stop_event=stop_event)
