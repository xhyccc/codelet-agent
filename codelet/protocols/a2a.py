"""Minimal Agent-to-Agent (A2A) HTTP server.

This is a small dependency-free server that publishes:

* ``GET /.well-known/agent.json`` - the agent card describing what this
  agent can do.
* ``POST /tasks/send`` - JSON-RPC-style "send a task" endpoint.  The
  request body is ``{"id":"...", "message": "..."}``; the response is
  ``{"id":"...", "status":"completed", "result":"..."}``.

The shape is intentionally a *very* lean subset of the A2A spec --
enough to demonstrate cross-agent calls and to be wired up to a more
complete implementation later.  No auth.  No streaming.  Single thread
per connection via the stdlib ``http.server``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional


@dataclass
class A2AAgentCard:
    """Subset of the A2A "agent card" descriptor."""

    name: str
    description: str
    version: str = "0.1.0"
    capabilities: List[str] = field(default_factory=lambda: ["tasks/send"])
    skills: List[Dict[str, Any]] = field(default_factory=list)


def build_agent_card(agent, *, name: str = "mini-coding-agent",
                     description: str = "Tiny CLI coding agent",
                     version: str = "0.1.0") -> A2AAgentCard:
    """Build an :class:`A2AAgentCard` summarising the agent's tools."""
    skills = []
    for tool_name, spec in agent.tools.items():
        skills.append({
            "name": tool_name,
            "description": spec.get("description", ""),
        })
    return A2AAgentCard(
        name=name,
        description=description,
        version=version,
        skills=skills,
    )


# Type alias for a "task handler": given the message string, return the result string.
TaskHandler = Callable[[str], str]


def make_a2a_app(card: A2AAgentCard, handler: TaskHandler) -> type:
    """Build a ``BaseHTTPRequestHandler`` subclass for the given card+handler.

    Returned as a class so callers can plug it into any HTTPServer they
    want (tests typically wrap it in :class:`ThreadingHTTPServer`).
    """
    card_dict = asdict(card)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003 - silencing
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path == "/.well-known/agent.json":
                self._json(200, card_dict)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path != "/tasks/send":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                self._json(400, {"error": f"invalid json: {exc}"})
                return
            task_id = payload.get("id") or "task-1"
            message = payload.get("message") or payload.get("prompt") or ""
            if not isinstance(message, str) or not message.strip():
                self._json(400, {"error": "message must be a non-empty string"})
                return
            try:
                result = handler(message)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"id": task_id, "status": "failed",
                                 "error": str(exc)})
                return
            self._json(200, {"id": task_id, "status": "completed",
                             "result": result})

    return _Handler


def serve_a2a_blocking(agent, *, host: str = "127.0.0.1", port: int = 0,
                       card: Optional[A2AAgentCard] = None,
                       blocking: bool = True):
    """Start a tiny A2A HTTP server.

    When ``blocking=True`` the call serves forever (Ctrl-C to stop).
    When ``blocking=False`` it spawns a daemon thread and returns
    ``(server, thread)`` so tests can reach in.
    """
    card = card or build_agent_card(agent)

    def _handler(message: str) -> str:
        return agent.ask(message)

    handler_cls = make_a2a_app(card, _handler)
    server = ThreadingHTTPServer((host, port), handler_cls)
    if blocking:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
