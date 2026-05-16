"""Minimal Model Context Protocol (MCP) client + server.

We deliberately implement only the subset of MCP needed for tool I/O:

* ``initialize`` handshake
* ``tools/list``
* ``tools/call``

This keeps the agent dependency-free.  Production users who need the full
protocol (resources, prompts, sampling, subscriptions) should install the
official ``mcp`` PyPI package and swap this module out.

Wire format: line-delimited JSON-RPC 2.0 over stdio (stdin/stdout).  This
is the original MCP transport.

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

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class MCPClientError(RuntimeError):
    """Raised on MCP protocol or transport failures."""


# ---------------------------------------------------------------------------
# Client (stdio JSON-RPC)
# ---------------------------------------------------------------------------


@dataclass
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict] = None


class MCPClient:
    """A tiny synchronous MCP client speaking line-delimited JSON-RPC 2.0.

    The client launches the server as a subprocess, drives the
    ``initialize`` handshake, lists tools, and proxies tool calls.  A
    background reader thread routes incoming responses to the matching
    request id.  All public methods are synchronous and thread-safe at
    the request granularity.
    """

    def __init__(self, name: str, command: List[str], env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None, timeout: float = 10.0):
        self.name = name
        self.command = list(command)
        self.env = env
        self.cwd = cwd
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: Dict[int, _PendingRequest] = {}
        self._reader: Optional[threading.Thread] = None
        self._stopped = threading.Event()

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            return
        full_env = dict(os.environ)
        if self.env:
            full_env.update(self.env)
        self._proc = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            cwd=self.cwd,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # MCP handshake: client sends `initialize`, server responds with its
        # capabilities. We send minimal capability advertisement.
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "mini-coding-agent", "version": "0.x"},
            },
        )
        # MCP says clients SHOULD send `initialized` notification after.
        self._notify("notifications/initialized", {})

    def stop(self) -> None:
        self._stopped.set()
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2.0)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False

    # ---- transport ------------------------------------------------------

    def _send(self, payload: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise MCPClientError("server not started")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise MCPClientError(f"send failed: {exc}") from exc

    def _read_loop(self) -> None:
        assert self._proc is not None
        stdout = self._proc.stdout
        if stdout is None:
            return
        while not self._stopped.is_set():
            line = stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            req_id = msg.get("id")
            if req_id is None:
                # Server-initiated notification; ignore for now.
                continue
            with self._lock:
                pending = self._pending.pop(req_id, None)
            if pending is not None:
                pending.response = msg
                pending.event.set()

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Optional[dict] = None) -> Any:
        with self._lock:
            req_id = self._next_id
            self._next_id += 1
            pending = _PendingRequest()
            self._pending[req_id] = pending
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        self._send(payload)
        if not pending.event.wait(timeout=self.timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise MCPClientError(f"timeout waiting for {method}")
        msg = pending.response or {}
        if "error" in msg:
            raise MCPClientError(f"{method}: {msg['error']}")
        return msg.get("result")

    # ---- high-level API ------------------------------------------------

    def list_tools(self) -> List[dict]:
        result = self.request("tools/list", {})
        if isinstance(result, dict):
            return list(result.get("tools") or [])
        return list(result or [])

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})


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

    When ``blocking=True`` the call returns only when stdin closes.  When
    ``blocking=False`` a background thread is started and a handle is
    returned for graceful shutdown.  ``stdin`` / ``stdout`` default to
    ``sys.stdin`` / ``sys.stdout``; tests can pass file-like objects to
    drive the server end-to-end without subprocesses.
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
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mini-coding-agent", "version": "0.x"},
                },
            }
        if method == "notifications/initialized":
            return None  # notification - no response
        if method == "tools/list":
            tools = []
            for name, spec in agent.tools.items():
                tools.append({
                    "name": name,
                    "description": spec.get("description", ""),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            k: {"type": "string", "description": str(v)}
                            for k, v in (spec.get("schema") or {}).items()
                        },
                    },
                })
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                result = agent.run_tool(tool_name, arguments)
            except Exception as exc:  # noqa: BLE001 - surface as error
                return {
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            return {
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": str(result)}]},
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
                msg = json.loads(line)
            except json.JSONDecodeError:
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
