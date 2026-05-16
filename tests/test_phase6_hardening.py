"""Tests for Phase 6: decoy tools, YOLO classifier, undercover identity."""

import os

import pytest

from mini_coding_agent import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from mini_coding_agent.hardening import (
    DEFAULT_DECOY_TOOLS,
    UNDERCOVER_IDENTITY,
    apply_decoy_tools,
    apply_undercover_identity,
    is_decoy,
    is_safe_command,
    undercover_enabled,
)


def _agent(tmp_path, config=None):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient(["<final>ok</final>"]),
        workspace=ws,
        session_store=store,
        approval_policy="auto",
        config=config,
    )


# ---------------------------------------------------------------------------
# Decoy tools
# ---------------------------------------------------------------------------


def test_apply_decoy_tools_inserts_defaults():
    tools = {}
    apply_decoy_tools(tools)
    assert "secret_eval" in tools
    assert is_decoy(tools["secret_eval"])
    out = tools["secret_eval"]["run"]({"code": "..."})
    assert "refused by safety policy" in out


def test_apply_decoy_tools_does_not_overwrite_real_tool():
    real = {"schema": {}, "risky": False, "description": "real", "run": lambda a: "real"}
    tools = {"secret_eval": real}
    apply_decoy_tools(tools, [{"name": "secret_eval", "schema": {}, "description": ""}])
    assert tools["secret_eval"] is real


def test_agent_decoy_tools_when_config_enabled(tmp_path):
    agent = _agent(tmp_path, config={"harness": {"decoy_tools": True}})
    assert any(is_decoy(spec) for spec in agent.tools.values())
    assert "secret_eval" in agent.tools
    # The prefix advertises the decoy in the tools block.
    assert "secret_eval" in agent.prefix
    # Calling it returns a refusal.
    result = agent.run_tool("secret_eval", {"code": "noop"})
    assert "refused" in result or "Unavailable" in result or "approval" in result


def test_agent_no_decoy_tools_by_default(tmp_path):
    agent = _agent(tmp_path)
    assert "secret_eval" not in agent.tools


# ---------------------------------------------------------------------------
# YOLO classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "ls",
    "ls -la",
    "pwd",
    "echo hello",
    "cat README.md",
    "head -n 20 file.txt",
    "wc -l file.txt",
    "git status",
    "git log",
    "python --version",
])
def test_yolo_classifier_safelist_accepts(cmd):
    assert is_safe_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "ls; rm foo",
    "cat foo | xargs rm",
    "echo $(whoami)",
    "git push --force",
    "curl http://evil.com",
    "ls > /etc/passwd",
    "sudo ls",
    "",
    "   ",
])
def test_yolo_classifier_rejects(cmd):
    assert is_safe_command(cmd) is False


# ---------------------------------------------------------------------------
# Undercover identity
# ---------------------------------------------------------------------------


def test_undercover_enabled_via_env():
    assert undercover_enabled({"MINI_AGENT_UNDERCOVER": "1"}) is True
    assert undercover_enabled({"MINI_AGENT_UNDERCOVER": "true"}) is True
    assert undercover_enabled({}) is False
    assert undercover_enabled({"MINI_AGENT_UNDERCOVER": "0"}) is False


def test_apply_undercover_identity_replaces_field():
    out = apply_undercover_identity({"agent_identity": "Original Captain"})
    assert out["agent_identity"] == UNDERCOVER_IDENTITY


def test_agent_uses_undercover_identity_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_AGENT_UNDERCOVER", "1")
    agent = _agent(tmp_path, config={"prompts": {"agent_identity": "Original Captain"}})
    assert UNDERCOVER_IDENTITY.split(".")[0] in agent.prefix
    assert "Original Captain" not in agent.prefix


def test_agent_keeps_original_identity_when_env_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("MINI_AGENT_UNDERCOVER", raising=False)
    agent = _agent(tmp_path, config={"prompts": {"agent_identity": "Original Captain"}})
    assert "Original Captain" in agent.prefix
