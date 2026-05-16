"""Tests for Phase 4: MCP client/server, A2A server, ACP stub."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from mini_coding_agent import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from mini_coding_agent.protocols import (
    A2AAgentCard,
    ACPSessionStub,
    MCPClient,
    MCPClientError,
    build_agent_card,
    discover_mcp_servers,
    load_mcp_config,
    register_mcp_tools,
    serve_a2a_blocking,
    serve_mcp_stdio,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_agent(tmp_path, scripted=None):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient(scripted or ["<final>ok</final>"]),
        workspace=ws,
        session_store=store,
        approval_policy="auto",
    )


# A trivial MCP echo server, runnable as a subprocess via `python -c`.
_MCP_ECHO_SERVER_SOURCE = r"""
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        out = {"jsonrpc":"2.0","id":req_id,"result":{
            "protocolVersion":"2024-11-05",
            "capabilities":{"tools":{}},
            "serverInfo":{"name":"echo","version":"0.1"}
        }}
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        out = {"jsonrpc":"2.0","id":req_id,"result":{"tools":[
            {"name":"echo","description":"echo arg back","inputSchema":{}}
        ]}}
    elif method == "tools/call":
        params = msg.get("params") or {}
        args = params.get("arguments") or {}
        text = "echo:" + str(args.get("text",""))
        out = {"jsonrpc":"2.0","id":req_id,"result":{"content":[{"type":"text","text":text}]}}
    else:
        out = {"jsonrpc":"2.0","id":req_id,"error":{"code":-32601,"message":"no"}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
"""


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------


def test_mcp_client_handshake_and_call():
    with MCPClient("echo", command=[sys.executable, "-c", _MCP_ECHO_SERVER_SOURCE],
                   timeout=5.0) as client:
        tools = client.list_tools()
        assert any(t.get("name") == "echo" for t in tools)
        result = client.call_tool("echo", {"text": "hi"})
        # Result follows MCP shape: {"content":[{"type":"text","text":"echo:hi"}]}
        assert isinstance(result, dict)
        text = result["content"][0]["text"]
        assert text == "echo:hi"


def test_mcp_client_timeout_on_silent_server():
    # Reads stdin into the void; never produces a response.
    silent_src = "import sys, time\nfor _ in sys.stdin:\n    pass\ntime.sleep(2)\n"
    client = MCPClient("silent", command=[sys.executable, "-c", silent_src], timeout=0.5)
    with pytest.raises(MCPClientError):
        client.start()
    client.stop()


def test_load_mcp_config_round_trip(tmp_path):
    cfg = {"servers": {"echo": {"command": ["python", "-m", "echo"]}}}
    cfg_path = tmp_path / "mcp.json"
    cfg_path.write_text(json.dumps(cfg))
    assert load_mcp_config(cfg_path) == cfg


def test_discover_mcp_servers_missing_returns_empty(tmp_path):
    assert discover_mcp_servers(tmp_path) == {}


def test_load_mcp_config_rejects_missing_command(tmp_path):
    bad = {"servers": {"bad": {}}}
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(MCPClientError):
        load_mcp_config(p)


def test_register_mcp_tools_adds_prefixed_tools(tmp_path):
    agent = _build_agent(tmp_path)
    cfg = {"servers": {"echo": {"command": [sys.executable, "-c", _MCP_ECHO_SERVER_SOURCE]}}}
    clients = register_mcp_tools(agent, cfg)
    try:
        assert "mcp__echo__echo" in agent.tools
        out = agent.tools["mcp__echo__echo"]["run"]({"text": "wired"})
        assert out == "echo:wired"
    finally:
        for c in clients:
            c.stop()


# ---------------------------------------------------------------------------
# MCP server (in-process via StringIO)
# ---------------------------------------------------------------------------


def test_mcp_server_handles_initialize_and_tools_list(tmp_path):
    agent = _build_agent(tmp_path)
    req_init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    req_list = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    stdin = io.StringIO(json.dumps(req_init) + "\n" + json.dumps(req_list) + "\n")
    stdout = io.StringIO()
    serve_mcp_stdio(agent, stdin=stdin, stdout=stdout, blocking=True)
    lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
    assert len(lines) == 2
    init_resp = json.loads(lines[0])
    list_resp = json.loads(lines[1])
    assert init_resp["result"]["serverInfo"]["name"] == "mini-coding-agent"
    tools = list_resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "read_file" in names


def test_mcp_server_tools_call_invokes_agent_tool(tmp_path):
    agent = _build_agent(tmp_path)
    (tmp_path / "hello.txt").write_text("greetings\n")
    req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
           "params": {"name": "read_file", "arguments": {"path": "hello.txt"}}}
    stdin = io.StringIO(json.dumps(req) + "\n")
    stdout = io.StringIO()
    serve_mcp_stdio(agent, stdin=stdin, stdout=stdout, blocking=True)
    resp = json.loads(stdout.getvalue().strip())
    assert resp["id"] == 7
    text = resp["result"]["content"][0]["text"]
    assert "greetings" in text


# ---------------------------------------------------------------------------
# A2A server
# ---------------------------------------------------------------------------


def test_a2a_agent_card_lists_tools(tmp_path):
    agent = _build_agent(tmp_path)
    card = build_agent_card(agent)
    assert isinstance(card, A2AAgentCard)
    skill_names = {s["name"] for s in card.skills}
    assert "read_file" in skill_names
    assert "tasks/send" in card.capabilities


def test_a2a_server_serves_agent_card_and_task(tmp_path):
    import urllib.request

    agent = _build_agent(tmp_path, scripted=["<final>a2a-pong</final>"])
    server, thread = serve_a2a_blocking(agent, host="127.0.0.1", port=0, blocking=False)
    try:
        port = server.server_address[1]
        # GET agent card.
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/agent.json",
                                    timeout=5.0) as resp:
            card = json.loads(resp.read().decode("utf-8"))
        assert card["name"] == "mini-coding-agent"

        # POST a task.
        body = json.dumps({"id": "t1", "message": "ping"}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/tasks/send",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        assert result["id"] == "t1"
        assert result["status"] == "completed"
        assert "a2a-pong" in result["result"]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# ACP stub
# ---------------------------------------------------------------------------


def test_acp_session_stub_round_trip():
    s = ACPSessionStub(session_id="abc")
    s.append_user_turn("hi")
    s.append_agent_turn("hello", tool_calls=[{"name": "noop", "args": {}}])
    d = s.to_dict()
    assert d["session_id"] == "abc"
    assert d["turns"][0] == {"role": "user", "content": "hi"}
    assert d["turns"][1]["tool_calls"][0]["name"] == "noop"
