"""Tests for Phase 2 tool additions: delete_file, move_file, patch diff,
Windows shell branching."""

from pathlib import Path

import pytest

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
)
from codelet.tools import _is_windows, _render_diff, _windows_shell_command


def _agent(tmp_path):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    client = FakeModelClient(["<final>noop</final>"])
    return MiniAgent(
        model_client=client, workspace=ws, session_store=store,
        approval_policy="auto",
    )


def test_render_diff_basic():
    out = _render_diff("a\nb\nc\n", "a\nB\nc\n", "x.txt")
    assert "x.txt" in out
    assert "-b" in out and "+B" in out


def test_render_diff_empty_for_identical():
    assert _render_diff("a\n", "a\n", "x.txt") == ""


def test_windows_shell_command_returns_list():
    cmd = _windows_shell_command("echo hi")
    assert isinstance(cmd, list)
    assert cmd[-1] == "echo hi"


def test_is_windows_returns_bool():
    assert isinstance(_is_windows(), bool)


def test_delete_file_moves_to_trash(tmp_path):
    agent = _agent(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("bye")
    result = agent.run_tool("delete_file", {"path": "victim.txt"})
    assert "trashed" in result
    assert not victim.exists()
    trash_root = tmp_path / ".mini-coding-agent" / "trash"
    entries = list(trash_root.rglob("*victim.txt"))
    assert len(entries) == 1
    assert entries[0].read_text() == "bye"


def test_delete_file_missing_path_errors(tmp_path):
    agent = _agent(tmp_path)
    out = agent.run_tool("delete_file", {"path": "ghost.txt"})
    assert out.startswith("error:")


def test_delete_file_refuses_nonempty_dir(tmp_path):
    agent = _agent(tmp_path)
    (tmp_path / "stuff").mkdir()
    (tmp_path / "stuff" / "thing").write_text("x")
    out = agent.run_tool("delete_file", {"path": "stuff"})
    assert out.startswith("error:")


def test_move_file_renames(tmp_path):
    agent = _agent(tmp_path)
    (tmp_path / "before.txt").write_text("hello")
    result = agent.run_tool("move_file", {"src": "before.txt", "dst": "after.txt"})
    assert "moved" in result
    assert not (tmp_path / "before.txt").exists()
    assert (tmp_path / "after.txt").read_text() == "hello"


def test_move_file_refuses_overwrite(tmp_path):
    agent = _agent(tmp_path)
    (tmp_path / "a").write_text("A")
    (tmp_path / "b").write_text("B")
    out = agent.run_tool("move_file", {"src": "a", "dst": "b"})
    assert out.startswith("error:")


def test_patch_file_renders_diff(tmp_path):
    agent = _agent(tmp_path)
    target = tmp_path / "code.py"
    target.write_text("x = 1\ny = 2\nz = 3\n")
    result = agent.run_tool(
        "patch_file",
        {"path": "code.py", "old_text": "y = 2", "new_text": "y = 22"},
    )
    assert "patched code.py" in result
    assert "-y = 2" in result
    assert "+y = 22" in result
    assert target.read_text() == "x = 1\ny = 22\nz = 3\n"
