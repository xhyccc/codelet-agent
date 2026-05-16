"""Tests for Phase 5: skills, remember_fact, subdir AGENT.md."""

from pathlib import Path

import pytest

from codelet import FakeModelClient, MiniAgent, SessionStore, WorkspaceContext
from codelet.skills import (
    discover_skills,
    load_skill_body,
    render_skill_manifest,
)


def _agent(tmp_path, scripted=None):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient(scripted or ["<final>ok</final>"]),
        workspace=ws,
        session_store=store,
        approval_policy="auto",
    )


def _make_skill(repo: Path, name: str, description: str, body: str = "") -> Path:
    sk_dir = repo / ".mini-coding-agent" / "skills" / name
    sk_dir.mkdir(parents=True)
    (sk_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return sk_dir


def test_discover_skills_parses_front_matter(tmp_path):
    _make_skill(tmp_path, "tidy", "Tidy up a Python project.",
                "Run black + isort, then summarise.")
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].name == "tidy"
    assert "Tidy up" in skills[0].description
    assert "Run black" in skills[0].body


def test_skill_manifest_renders_compact_lines(tmp_path):
    _make_skill(tmp_path, "alpha", "do alpha")
    _make_skill(tmp_path, "bravo", "do bravo")
    manifest = render_skill_manifest(discover_skills(tmp_path))
    assert "- alpha: do alpha" in manifest
    assert "- bravo: do bravo" in manifest


def test_load_skill_body_returns_full_text(tmp_path):
    _make_skill(tmp_path, "gamma", "do gamma", body="Step 1. Step 2.")
    skills = discover_skills(tmp_path)
    body = load_skill_body(skills, "gamma")
    assert "Skill: gamma" in body
    assert "Step 1" in body


def test_load_skill_unknown_returns_error(tmp_path):
    assert load_skill_body([], "nope").startswith("error:")


def test_agent_registers_load_skill_when_skills_present(tmp_path):
    _make_skill(tmp_path, "alpha", "do alpha", body="hello world")
    agent = _agent(tmp_path)
    assert "load_skill" in agent.tools
    result = agent.run_tool("load_skill", {"name": "alpha"})
    assert "hello world" in result


def test_agent_omits_load_skill_when_no_skills(tmp_path):
    agent = _agent(tmp_path)
    assert "load_skill" not in agent.tools


def test_skill_manifest_appended_to_prefix(tmp_path):
    _make_skill(tmp_path, "alpha", "do alpha")
    agent = _agent(tmp_path)
    assert "alpha: do alpha" in agent.prefix


def test_remember_fact_appends_repo_memory(tmp_path):
    agent = _agent(tmp_path)
    result = agent.run_tool("remember_fact", {"fact": "TestX uses pytest-cov"})
    assert "remembered" in result
    memory_path = tmp_path / ".mini-coding-agent" / "repo-memory.md"
    assert memory_path.is_file()
    content = memory_path.read_text(encoding="utf-8")
    assert "- TestX uses pytest-cov" in content
    # Second fact appends without overwriting.
    agent.run_tool("remember_fact", {"fact": "Builds need Python 3.11"})
    content = memory_path.read_text(encoding="utf-8")
    assert "TestX uses pytest-cov" in content
    assert "Builds need Python 3.11" in content


def test_remember_fact_rejects_empty(tmp_path):
    agent = _agent(tmp_path)
    out = agent.run_tool("remember_fact", {"fact": "   "})
    assert out.startswith("error:")


def test_subdir_agent_md_auto_loaded_on_tool_call(tmp_path):
    sub = tmp_path / "src" / "feature_x"
    sub.mkdir(parents=True)
    (sub / "AGENT.md").write_text("Convention: feature_x uses tabs.\n", encoding="utf-8")
    (sub / "code.py").write_text("x = 1\n", encoding="utf-8")
    agent = _agent(tmp_path)
    agent.run_tool("read_file", {"path": "src/feature_x/code.py", "start": 1, "end": 1})
    notes = agent.session["memory"]["notes"]
    assert any("src/feature_x/AGENT.md" in n for n in notes)
    # Second call: not loaded again (idempotent).
    before = list(notes)
    agent.run_tool("read_file", {"path": "src/feature_x/code.py", "start": 1, "end": 1})
    assert list(agent.session["memory"]["notes"]) == before
