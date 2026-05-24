"""Integration tests for the progressive-disclosure skills system.

These tests exercise the full skill lifecycle with a real LLM backend.
They require API credentials provided via environment variables or a .env file.

Run with:
    python -m pytest tests/test_integration_skills.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codelet import MiniAgent, OpenAIModelClient, SessionStore, WorkspaceContext
from codelet.skills import discover_skills, load_skill_body, render_skill_manifest

# Import shared markers
from conftest import requires_api_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skill(repo: Path, name: str, description: str, body: str = "") -> Path:
    """Create a skill directory with a SKILL.md file."""
    sk_dir = repo / ".codelet" / "skills" / name
    sk_dir.mkdir(parents=True, exist_ok=True)
    (sk_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    return sk_dir


def _build_agent(tmp_path, model_client, *, skills=None):
    """Build a MiniAgent with optional skills pre-created."""
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    if skills:
        for name, desc, body in skills:
            _make_skill(tmp_path, name, desc, body)
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".codelet" / "sessions")
    return MiniAgent(
        model_client=model_client,
        workspace=ws,
        session_store=store,
        approval_policy="auto",
        max_steps=5,
    )


# ---------------------------------------------------------------------------
# Tests: Skill discovery (no LLM needed)
# ---------------------------------------------------------------------------


class TestSkillDiscovery:
    """Verify skill discovery and manifest rendering work end-to-end."""

    def test_discover_skills_finds_valid_skills(self, tmp_path):
        _make_skill(tmp_path, "linter", "Run project linters.", "Run: black + isort.")
        _make_skill(tmp_path, "docs", "Generate API docs.", "Use sphinx-build.")
        skills = discover_skills(tmp_path)
        assert len(skills) == 2
        names = {s.name for s in skills}
        assert names == {"docs", "linter"}

    def test_discover_skills_empty_when_no_skill_dir(self, tmp_path):
        assert discover_skills(tmp_path) == []

    def test_discover_skills_ignores_non_directories(self, tmp_path):
        base = tmp_path / ".codelet" / "skills"
        base.mkdir(parents=True)
        (base / "random_file.txt").write_text("not a skill")
        assert discover_skills(tmp_path) == []

    def test_skill_with_assets(self, tmp_path):
        sk_dir = _make_skill(tmp_path, "deploy", "Deploy to prod.", "Follow steps.")
        (sk_dir / "template.yaml").write_text("apiVersion: v1\n")
        (sk_dir / "scripts" ).mkdir()
        (sk_dir / "scripts" / "run.sh").write_text("#!/bin/bash\necho deploy")
        skills = discover_skills(tmp_path)
        assert len(skills) == 1
        assert "template.yaml" in skills[0].assets
        assert "scripts/run.sh" in skills[0].assets

    def test_manifest_rendering(self, tmp_path):
        _make_skill(tmp_path, "alpha", "Do alpha things.")
        _make_skill(tmp_path, "beta", "Do beta things.")
        skills = discover_skills(tmp_path)
        manifest = render_skill_manifest(skills)
        assert "<skills>" in manifest
        assert "- alpha: Do alpha things." in manifest
        assert "- beta: Do beta things." in manifest
        assert "load_skill" in manifest

    def test_load_skill_body_returns_content(self, tmp_path):
        _make_skill(tmp_path, "refactor", "Refactor code.", "Step 1: Identify targets.")
        skills = discover_skills(tmp_path)
        body = load_skill_body(skills, "refactor")
        assert "Skill: refactor" in body
        assert "Step 1: Identify targets." in body

    def test_load_skill_body_unknown_skill(self, tmp_path):
        _make_skill(tmp_path, "real", "A real skill.")
        skills = discover_skills(tmp_path)
        result = load_skill_body(skills, "nonexistent")
        assert result.startswith("error:")


# ---------------------------------------------------------------------------
# Tests: Agent integration with skills (no LLM needed)
# ---------------------------------------------------------------------------


class TestAgentSkillRegistration:
    """Verify the agent registers load_skill when skills are present."""

    def test_load_skill_tool_registered_when_skills_exist(self, tmp_path):
        from codelet import FakeModelClient

        agent = _build_agent(
            tmp_path,
            FakeModelClient(["<final>ok</final>"]),
            skills=[("helper", "A helper skill.", "Do helpful things.")],
        )
        assert "load_skill" in agent.tools
        assert agent.skills
        assert agent.skills[0].name == "helper"

    def test_load_skill_tool_not_registered_without_skills(self, tmp_path):
        from codelet import FakeModelClient

        agent = _build_agent(tmp_path, FakeModelClient(["<final>ok</final>"]))
        assert "load_skill" not in agent.tools

    def test_skill_manifest_in_agent_prefix(self, tmp_path):
        from codelet import FakeModelClient

        agent = _build_agent(
            tmp_path,
            FakeModelClient(["<final>ok</final>"]),
            skills=[("checker", "Check code style.", "Run flake8.")],
        )
        assert "checker: Check code style." in agent.prefix


# ---------------------------------------------------------------------------
# Tests: End-to-end with real LLM (requires API key)
# ---------------------------------------------------------------------------


@requires_api_key
class TestSkillsWithRealLLM:
    """Integration tests using a real LLM to exercise the skill tool call flow."""

    @pytest.fixture()
    def model_client(self):
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("LLM_MODEL") or "gpt-4o-mini"
        base_url = os.environ.get("LLM_BASE_URL") or None
        return OpenAIModelClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=0.0,
            top_p=1.0,
            timeout=60,
        )

    def test_agent_can_load_skill_via_tool(self, tmp_path, model_client):
        """Agent should be able to load a skill using the load_skill tool."""
        agent = _build_agent(
            tmp_path,
            model_client,
            skills=[("summarizer", "Summarize text.", "Read the input and produce a concise summary.")],
        )
        # Directly call the tool (bypasses the LLM loop) to verify wiring
        result = agent.run_tool("load_skill", {"name": "summarizer"})
        assert "Skill: summarizer" in result
        assert "concise summary" in result

    def test_agent_ask_triggers_skill_awareness(self, tmp_path, model_client):
        """When skills exist, the agent's prefix should mention them.

        We ask the agent a trivial question and verify it responds
        (proving the LLM connection works end-to-end).
        """
        agent = _build_agent(
            tmp_path,
            model_client,
            skills=[("formatter", "Format code with black.", "Run black on all .py files.")],
        )
        # The prefix should contain skill info
        assert "formatter" in agent.prefix

        # Ask a simple question that doesn't require tool use
        stop_reason = agent.ask("What skills are available? Just list the skill names.")
        # The agent should complete without error
        assert stop_reason is not None

    def test_agent_run_tool_load_skill_not_found(self, tmp_path, model_client):
        """load_skill with unknown name returns an error string."""
        agent = _build_agent(
            tmp_path,
            model_client,
            skills=[("real-skill", "A real skill.", "Body.")],
        )
        result = agent.run_tool("load_skill", {"name": "ghost-skill"})
        assert "error" in result.lower()
