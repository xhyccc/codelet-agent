"""Tests for Phase 3: decompose and delegate_parallel tools."""

import json

import pytest

from mini_coding_agent import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
)


def _agent(tmp_path, scripted=None):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    client = FakeModelClient(scripted or ["<final>noop</final>"])
    return MiniAgent(
        model_client=client, workspace=ws, session_store=store,
        approval_policy="auto",
    )


def test_decompose_records_plan_with_explicit_steps(tmp_path):
    agent = _agent(tmp_path)
    result = agent.run_tool(
        "decompose",
        {"goal": "refactor X", "steps": ["read X", "rewrite X", "test X"]},
    )
    assert "plan recorded" in result
    assert "1. read X" in result
    assert agent.session["plan"]["goal"] == "refactor X"
    assert agent.session["plan"]["steps"] == ["read X", "rewrite X", "test X"]


def test_decompose_autosplits_goal_string(tmp_path):
    agent = _agent(tmp_path)
    result = agent.run_tool(
        "decompose",
        {"goal": "open file. patch bug. run tests."},
    )
    assert "plan recorded" in result
    steps = agent.session["plan"]["steps"]
    assert len(steps) >= 2


def test_decompose_requires_goal(tmp_path):
    agent = _agent(tmp_path)
    out = agent.run_tool("decompose", {"goal": ""})
    assert out.startswith("error:")


def test_delegate_parallel_runs_two_children(tmp_path):
    # Each child agent gets a fresh FakeModelClient via the parent. Since
    # FakeModelClient is stateful per-instance and shared here, we need a
    # client that just keeps returning the same final answer.
    class Replay:
        def __init__(self, response):
            self._response = response
        def complete(self, prompt, n):
            return self._response

    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    client = Replay("<final>child answered</final>")
    agent = MiniAgent(
        model_client=client, workspace=ws, session_store=store,
        approval_policy="auto", max_depth=1,
    )
    result = agent.run_tool(
        "delegate_parallel",
        {"tasks": ["task A", "task B"], "max_steps": 1},
    )
    assert result.startswith("delegate_parallel_result:")
    payload = json.loads(result.split("\n", 1)[1])
    assert {entry["task"] for entry in payload} == {"task A", "task B"}
    for entry in payload:
        assert "child answered" in entry.get("result", "")
    # A log file should have been written.
    logs = list((tmp_path / ".mini-coding-agent" / "delegated").glob("*.json"))
    assert len(logs) == 1


def test_delegate_parallel_rejects_empty(tmp_path):
    agent = _agent(tmp_path)
    out = agent.run_tool("delegate_parallel", {"tasks": []})
    assert out.startswith("error:")
