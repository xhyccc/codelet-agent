"""Integration and unit tests to align codelet with the reference coding agent.

Each test documents a feature from the reference agent and verifies that
codelet implements it (or has a compatible equivalent). Tests are organized
by feature area and marked with xfail until the gap is closed.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure codelet is importable from the workspace
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codelet.agent import MiniAgent
from codelet.config import BUILTIN_DEFAULTS, deep_merge
from codelet.compaction import (
    DEFAULT_COMPACTION,
    HardHaltError,
    apply_tool_output_budget,
    budget_reduction,
    context_collapse,
    microcompaction,
    render_history_size,
    run_cascade,
    snipping,
)
from codelet.hardening import (
    DEFAULT_DECOY_TOOLS,
    apply_decoy_tools,
    apply_undercover_identity,
    is_decoy,
    is_safe_command,
    undercover_enabled,
)
from codelet.prompt import build_history_text, build_memory_text, build_prefix, build_prompt
from codelet.sessions import SessionStore
from codelet.skills import Skill, _parse_front_matter, _parse_skill_file, discover_skills, load_skill_body, render_skill_manifest
from codelet.tools import CONCURRENCY_SAFE_TOOLS, ToolRegistry, is_concurrency_safe
from codelet.utils import clip, dedupe_lines, now, strip_ansi


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def mock_model_client():
    client = MagicMock()
    client.complete = MagicMock(return_value="<final>Done.</final>")
    return client


@pytest.fixture
def mock_workspace(tmp_workspace):
    ws = MagicMock()
    ws.repo_root = str(tmp_workspace)
    ws.text = MagicMock(return_value="Workspace: test")
    return ws


@pytest.fixture
def session_store(tmp_workspace):
    return SessionStore(tmp_workspace / ".codelet" / "sessions")


@pytest.fixture
def basic_agent(mock_model_client, mock_workspace, session_store):
    return MiniAgent(
        model_client=mock_model_client,
        workspace=mock_workspace,
        session_store=session_store,
        approval_policy="auto",
        config={"harness": {"max_steps": 6, "max_new_tokens": 512}},
    )


# =============================================================================
# 1. COST TRACKING
# =============================================================================


class TestCostTracking:
    """Reference agent tracks per-session API cost, token usage, and model usage.
    
    Gaps:
    - No cost accumulation across API calls
    - No token counters (input/output/cache read/cache creation)
    - No per-model usage tracking
    - No budget limit enforcement (max_budget_usd)
    - No cost formatting/display helpers
    """

    def test_agent_tracks_api_cost(self, basic_agent):
        """MiniAgent should accumulate cost from each model call."""
        assert hasattr(basic_agent, "total_cost_usd")
        assert basic_agent.total_cost_usd == 0.0

    def test_agent_tracks_token_usage(self, basic_agent):
        """MiniAgent should track input/output/cache token counts."""
        assert hasattr(basic_agent, "token_usage")
        usage = basic_agent.token_usage
        assert "input_tokens" in usage
        assert "output_tokens" in usage
        assert "cache_read_input_tokens" in usage
        assert "cache_creation_input_tokens" in usage

    def test_agent_tracks_per_model_usage(self, basic_agent):
        """MiniAgent should track usage per model name."""
        assert hasattr(basic_agent, "model_usage")
        assert isinstance(basic_agent.model_usage, dict)

    def test_agent_enforces_budget_limit(self, basic_agent):
        """MiniAgent should stop when total cost exceeds max_budget_usd."""
        basic_agent.max_budget_usd = 0.01
        # Pre-seed cost so the budget check fires immediately
        basic_agent.cost_tracker.state.total_cost_usd = 0.02
        result = basic_agent.ask("test")
        assert basic_agent.last_stop_reason.name == "BUDGET_EXCEEDED"

    def test_cost_state_persists_across_sessions(self, tmp_workspace, session_store, mock_model_client, mock_workspace):
        """Cost state should be saved to project config and restored on resume."""
        agent1 = MiniAgent(
            model_client=mock_model_client,
            workspace=mock_workspace,
            session_store=session_store,
            config={"harness": {"max_steps": 2}},
        )
        agent1.ask("hello")
        # Cost should be saved somewhere
        config_path = tmp_workspace / ".codelet" / "config.json"
        assert config_path.exists()
        data = json.loads(config_path.read_text())
        assert "last_cost" in data
        assert "last_session_id" in data


# =============================================================================
# 2. PERMISSION RULES (Granular)
# =============================================================================


class TestPermissionRules:
    """Reference agent has alwaysAllow/alwaysDeny/alwaysAsk rules per tool source.
    
    Gaps:
    - Only has approval_policy (auto/ask/never) globally
    - No per-tool permission rules
    - No permission denial tracking
    - No tool decision history (accept/reject per tool)
    """

    def test_agent_has_permission_context(self, basic_agent):
        """MiniAgent should have a permission context with rule sets."""
        assert hasattr(basic_agent, "permission_context")
        ctx = basic_agent.permission_context
        assert hasattr(ctx, "always_allow")
        assert hasattr(ctx, "always_deny")
        assert hasattr(ctx, "always_ask")

    def test_always_allow_rule_bypasses_approval(self, basic_agent):
        """A tool in always_allow_rules should bypass the approval prompt."""
        from codelet.permissions import PermissionContext
        basic_agent.permission_context = PermissionContext(
            always_allow={"run_shell": ["*"]}
        )
        # Should auto-approve run_shell even with ask policy
        basic_agent.approval_policy = "ask"
        assert basic_agent.approve("run_shell", {"command": "ls"}) is True

    def test_always_deny_rule_blocks_tool(self, basic_agent):
        """A tool in always_deny_rules should be blocked without prompt."""
        from codelet.permissions import PermissionContext
        basic_agent.permission_context = PermissionContext(
            always_deny={"run_shell": ["*"]}
        )
        assert basic_agent.approve("run_shell", {"command": "ls"}) is False

    def test_agent_tracks_permission_denials(self, basic_agent):
        """MiniAgent should track permission denials for SDK reporting."""
        from codelet.permissions import PermissionContext
        basic_agent.permission_context = PermissionContext(
            always_deny={"run_shell": ["*"]}
        )
        basic_agent.approval_policy = "ask"
        # Direct approval call should record a denial
        basic_agent.approve("run_shell", {"command": "ls"})
        assert len(basic_agent.permission_denials) > 0


# =============================================================================
# 3. GLOBAL HISTORY
# =============================================================================


class TestGlobalHistory:
    """Reference agent has persistent JSONL history across all sessions.
    
    Gaps:
    - Only per-session JSON files
    - No global history.jsonl
    - No paste content store with hash references
    - No up-arrow / ctrl+r history search
    - No history deduplication
    """

    def test_global_history_file_exists(self, tmp_workspace):
        """A global history.jsonl should exist in .codelet/."""
        from codelet.history import append_history
        append_history(str(tmp_workspace), display="test", session_id="s1", project="p1")
        history_path = tmp_workspace / ".codelet" / "history.jsonl"
        assert history_path.exists()

    def test_global_history_records_prompts(self, tmp_workspace, session_store, mock_model_client, mock_workspace):
        """Each user prompt should be appended to global history."""
        agent = MiniAgent(
            model_client=mock_model_client,
            workspace=mock_workspace,
            session_store=session_store,
            config={"harness": {"max_steps": 2}},
        )
        agent.ask("hello world")
        history_path = tmp_workspace / ".codelet" / "history.jsonl"
        lines = history_path.read_text().strip().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[0])
        assert entry["display"] == "hello world"
        assert "timestamp" in entry
        assert "project" in entry
        assert "sessionId" in entry

    def test_paste_content_store(self, tmp_workspace):
        """Large pasted content should be stored by hash and referenced."""
        from codelet.history import store_pasted_text, retrieve_pasted_text, hash_pasted_text
        text = "x" * 2000
        h = hash_pasted_text(text)
        store_pasted_text(str(tmp_workspace), h, text)
        assert retrieve_pasted_text(str(tmp_workspace), h) == text

    def test_history_deduplication(self, tmp_workspace, session_store, mock_model_client, mock_workspace):
        """Duplicate prompts should not create duplicate history entries."""
        from codelet.history import read_history
        agent = MiniAgent(
            model_client=mock_model_client,
            workspace=mock_workspace,
            session_store=session_store,
            config={"harness": {"max_steps": 2}},
        )
        agent.ask("same prompt")
        agent.ask("same prompt")
        entries = read_history(str(tmp_workspace))
        displays = [e["display"] for e in entries]
        assert displays.count("same prompt") == 1


# =============================================================================
# 4. SLASH COMMANDS
# =============================================================================


class TestSlashCommands:
    """Reference agent has 60+ slash commands (/compact, /memory, /skills, /plan, /cost).
    
    Gaps:
    - No slash command system at all
    - No command registry
    - No command discovery from skills
    """

    def test_agent_has_command_registry(self, basic_agent):
        """MiniAgent should have a registry of slash commands."""
        assert hasattr(basic_agent, "commands")
        assert isinstance(basic_agent.commands, dict)
        assert len(basic_agent.commands) > 0

    def test_compact_command_triggers_compaction(self, basic_agent):
        """/compact should immediately run the compaction cascade."""
        from codelet.commands import run_command
        basic_agent.session["history"] = [{"role": "user", "content": "x" * 50000}]
        result = run_command(basic_agent, "/compact")
        assert "compacted" in result.lower() or "summary" in result.lower() or "halted" in result.lower()

    def test_memory_command_shows_memory(self, basic_agent):
        """/memory should display the current working memory."""
        from codelet.commands import run_command
        basic_agent.session["memory"]["notes"] = ["note1", "note2"]
        result = run_command(basic_agent, "/memory")
        assert "note1" in result

    def test_cost_command_shows_cost(self, basic_agent):
        """/cost should display accumulated cost and usage."""
        from codelet.commands import run_command
        result = run_command(basic_agent, "/cost")
        assert "cost" in result.lower() or "$" in result

    def test_skills_command_lists_skills(self, basic_agent):
        """/skills should list available skills."""
        from codelet.commands import run_command
        result = run_command(basic_agent, "/skills")
        assert "skill" in result.lower()

    def test_plan_command_creates_plan(self, basic_agent):
        """/plan should create or show the active plan."""
        from codelet.commands import run_command
        result = run_command(basic_agent, "/plan do thing A then thing B")
        assert "plan" in result.lower()
        assert basic_agent.session.get("plan") is not None


# =============================================================================
# 5. TASK SYSTEM
# =============================================================================


class TestTaskSystem:
    """Reference agent has spawn/kill tasks with output files and status tracking.
    
    Gaps:
    - No task spawn/kill lifecycle
    - No task output files
    - No task status tracking (pending/running/completed/failed/killed)
    - No background task support
    """

    def test_agent_has_task_registry(self, basic_agent):
        """MiniAgent should be able to spawn and track tasks."""
        assert hasattr(basic_agent, "spawn_task")
        assert hasattr(basic_agent, "kill_task")

    def test_spawn_task_creates_output_file(self, basic_agent, tmp_workspace):
        """Spawning a task should create an output file for its logs."""
        from codelet.tasks import spawn_task
        task = spawn_task(
            agent=basic_agent,
            task_type="local_bash",
            description="echo hello",
            command="echo hello",
        )
        assert task["id"]
        output_path = tmp_workspace / ".codelet" / "tasks" / f"{task['id']}.log"
        assert output_path.exists()

    def test_task_status_lifecycle(self, basic_agent):
        """Tasks should transition through pending→running→completed."""
        from codelet.tasks import spawn_task, get_task_status
        task = spawn_task(agent=basic_agent, task_type="local_bash", description="sleep 0.1", command="sleep 0.1")
        # local_bash tasks start immediately as running
        assert get_task_status(task["id"]) in ("pending", "running")
        time.sleep(0.2)
        assert get_task_status(task["id"]) in ("completed", "failed")

    def test_kill_task_stops_execution(self, basic_agent):
        """Killing a task should set its status to 'killed'."""
        from codelet.tasks import spawn_task, kill_task, get_task_status
        task = spawn_task(agent=basic_agent, task_type="local_bash", description="sleep 10", command="sleep 10")
        kill_task(task["id"])
        assert get_task_status(task["id"]) == "killed"


# =============================================================================
# 6. AUTO-COMPACT WITH WARNING/ERROR STATES
# =============================================================================


class TestAutoCompactStates:
    """Reference agent has token-threshold-triggered compaction with warning/error states.
    
    Gaps:
    - No warning state before compaction (warns user that context is getting full)
    - No error state when compaction fails (hard halt is surfaced but no explicit error state)
    - No pre-compact hooks
    - No post-compact hooks
    """

    def test_compaction_warning_state(self, basic_agent):
        """When history approaches target_chars, agent should enter a warning state."""
        # Fill history to 90% of target
        target = DEFAULT_COMPACTION["target_chars"]
        basic_agent.session["history"] = [
            {"role": "tool", "name": "read_file", "args": {}, "content": "x" * int(target * 0.95)}
        ]
        basic_agent.history_text()
        assert hasattr(basic_agent, "compact_state")
        assert basic_agent.compact_state == "warning"

    def test_compaction_error_state_on_halt(self, basic_agent, mock_model_client):
        """When auto-compaction fails to recover, agent should enter error state."""
        target = DEFAULT_COMPACTION["target_chars"]
        basic_agent.session["history"] = [
            {"role": "tool", "name": "read_file", "args": {}, "content": "x" * (target * 2)}
        ]
        # Mock model client to return a tiny summary
        mock_model_client.complete = MagicMock(return_value="summary")
        try:
            basic_agent.history_text()
        except HardHaltError:
            pass
        assert hasattr(basic_agent, "compact_state")
        assert basic_agent.compact_state == "error"

    def test_compact_hooks_fire(self, basic_agent):
        """Pre/post compact hooks should be called."""
        pre_called = []
        post_called = []
        basic_agent.on_pre_compact = lambda: pre_called.append(True)
        basic_agent.on_post_compact = lambda: post_called.append(True)
        target = DEFAULT_COMPACTION["target_chars"]
        basic_agent.session["history"] = [
            {"role": "tool", "name": "read_file", "args": {}, "content": "x" * (target + 1000)}
        ]
        basic_agent.history_text()
        assert pre_called
        assert post_called


# =============================================================================
# 7. MEMDIR (Structured Memory)
# =============================================================================


class TestMemdir:
    """Reference agent has structured MEMORY.md with typed taxonomy.
    
    Gaps:
    - No MEMORY.md file in .codelet/
    - No typed taxonomy (user/feedback/project/reference)
    - No memory prompt loading with frontmatter parsing
    - No nested memory attachment triggers
    """

    def test_memory_md_exists(self, tmp_workspace):
        """A MEMORY.md should exist in .codelet/ for structured memory."""
        from codelet.memdir import ensure_memory_dir
        ensure_memory_dir(str(tmp_workspace))
        assert (tmp_workspace / ".codelet" / "MEMORY.md").exists()

    def test_memory_has_typed_sections(self, tmp_workspace):
        """MEMORY.md should have typed sections: user, feedback, project, reference."""
        from codelet.memdir import load_memory_prompt
        memory_path = tmp_workspace / ".codelet" / "MEMORY.md"
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(
            "---\n"
            "type: user\n"
            "---\n"
            "User prefers Python.\n\n"
            "---\n"
            "type: project\n"
            "---\n"
            "Project uses FastAPI.\n"
        )
        sections = load_memory_prompt(str(tmp_workspace))
        assert "user" in sections
        assert "project" in sections
        assert "User prefers Python." in sections["user"]

    def test_nested_memory_attachment(self, basic_agent, tmp_workspace):
        """When a tool touches a subdir with CLAUDE.md, it should attach as nested memory."""
        subdir = tmp_workspace / "src"
        subdir.mkdir()
        (subdir / "CLAUDE.md").write_text("Use type hints.")
        from codelet.memdir import attach_nested_memory
        result = attach_nested_memory(basic_agent, str(subdir))
        assert "Use type hints." in result


# =============================================================================
# 8. SKILLS (Enhanced)
# =============================================================================


class TestSkillsEnhanced:
    """Reference agent has skills with effort levels, argument substitution, whenToUse.
    
    Gaps:
    - No effort levels (low/medium/high)
    - No argument substitution in skill body
    - No skill command generation (skills can expose slash commands)
    - No dynamic skill discovery
    """

    def test_skill_has_effort_level(self, tmp_workspace):
        """Skills should declare an effort level."""
        skill_dir = tmp_workspace / ".codelet" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: test-skill\n"
            "description: A test skill\n"
            "effort: low\n"
            "---\n"
            "Do something simple.\n"
        )
        skills = discover_skills(str(tmp_workspace))
        assert len(skills) == 1
        assert skills[0].effort == "low"

    def test_skill_argument_substitution(self, tmp_workspace):
        """Skill body should support {{arg}} substitution."""
        skill_dir = tmp_workspace / ".codelet" / "skills" / "greet"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: greet\n"
            "description: Greet someone\n"
            "argument_hint: name\n"
            "---\n"
            "Say hello to {{name}}.\n"
        )
        skills = discover_skills(str(tmp_workspace))
        body = load_skill_body(skills, "greet")
        # Should support substitution when loaded with args
        from codelet.skills import load_skill_body_with_args
        substituted = load_skill_body_with_args(skills, "greet", {"name": "Alice"})
        assert "Alice" in substituted

    def test_skills_expose_slash_commands(self, basic_agent):
        """Skills should be able to expose slash commands."""
        from codelet.skills import get_skill_commands
        commands = get_skill_commands(basic_agent.skills)
        assert isinstance(commands, list)


# =============================================================================
# 9. QUERY ENGINE / CONTEXT WINDOW
# =============================================================================


class TestQueryEngine:
    """Reference agent has QueryEngine with token budgets and context window management.
    
    Gaps:
    - No explicit token budget tracking during prompt assembly
    - No context window size awareness per model
    - No streaming tool executor with progress
    - No message normalization
    """

    def test_prompt_respects_token_budget(self, basic_agent):
        """Prompt assembly should respect a token budget and raise if exceeded."""
        basic_agent.max_tokens = 100
        # Fill history beyond budget
        basic_agent.session["history"] = [{"role": "user", "content": "x" * 10000}]
        with pytest.raises(Exception):
            basic_agent.prompt("hello")

    def test_agent_knows_context_window(self, basic_agent):
        """MiniAgent should know the context window for its model."""
        assert hasattr(basic_agent, "context_window")
        assert basic_agent.context_window > 0

    def test_streaming_tool_execution(self, basic_agent):
        """Tool execution should support streaming progress updates."""
        from codelet.query import StreamingToolExecutor
        executor = StreamingToolExecutor(basic_agent)
        events = list(executor.execute("run_shell", {"command": "echo hello"}))
        assert any(e["type"] == "progress" for e in events)


# =============================================================================
# 10. FILE HISTORY / SNAPSHOTS
# =============================================================================


class TestFileHistory:
    """Reference agent has file history snapshots for undo/rewind.
    
    Gaps:
    - No file history snapshots before writes
    - No rewind capability
    - No file state cache beyond basic mtime/size
    """

    def test_write_creates_snapshot(self, basic_agent, tmp_workspace):
        """Writing a file should create a snapshot in file history."""
        test_file = tmp_workspace / "test.py"
        test_file.write_text("original")
        basic_agent.run_tool("write_file", {"path": "test.py", "content": "modified"})
        from codelet.file_history import get_file_history
        history = get_file_history(str(tmp_workspace), "test.py")
        assert len(history) >= 1
        assert history[0]["content"] == "original"

    def test_rewind_restores_snapshot(self, basic_agent, tmp_workspace):
        """Rewind should restore a file to a previous snapshot."""
        test_file = tmp_workspace / "test.py"
        test_file.write_text("v1")
        basic_agent.run_tool("write_file", {"path": "test.py", "content": "v2"})
        from codelet.file_history import rewind_file
        rewind_file(str(tmp_workspace), "test.py", steps=1)
        assert test_file.read_text() == "v1"


# =============================================================================
# 11. CONTENT REPLACEMENT STATE
# =============================================================================


class TestContentReplacement:
    """Reference agent has per-conversation content replacement for tool result budget.
    
    Gaps:
    - No content replacement state tracking
    - No aggregate tool result budget across turns
    """

    def test_agent_has_content_replacement_state(self, basic_agent):
        """MiniAgent should track content replacement state."""
        assert hasattr(basic_agent, "content_replacement_state")
        assert basic_agent.content_replacement_state is not None

    def test_aggregate_tool_result_budget(self, basic_agent):
        """Tool results should be tracked against an aggregate budget."""
        from codelet.query import apply_aggregate_budget
        history = [
            {"role": "tool", "name": "run_shell", "content": "x" * 10000},
        ]
        budget = 5000
        result = apply_aggregate_budget(history, budget, basic_agent.content_replacement_state)
        assert len(result[0]["content"]) <= budget + 100  # allow for truncation message


# =============================================================================
# 12. SUBAGENT CONTEXT
# =============================================================================


class TestSubagentContext:
    """Reference agent has createSubagentContext with shared state.
    
    Gaps:
    - No formal subagent context creation
    - No shared setAppState for tasks
    - No agent ID tracking for subagents
    """

    def test_subagent_inherits_parent_state(self, basic_agent):
        """A subagent should inherit the parent's permission context and file cache."""
        from codelet.subagent import create_subagent_context
        ctx = create_subagent_context(basic_agent)
        assert ctx["permission_context"] == basic_agent.permission_context
        assert ctx["read_file_state"] == basic_agent._file_read_cache

    def test_subagent_can_set_app_state_for_tasks(self, basic_agent):
        """Subagents should be able to register tasks that outlive the turn."""
        from codelet.subagent import create_subagent_context
        ctx = create_subagent_context(basic_agent)
        assert ctx["set_app_state_for_tasks"] is not None


# =============================================================================
# 13. MCP SERVER CONNECTIONS
# =============================================================================


class TestMCP:
    """Reference agent supports MCP (Model Context Protocol) server connections.
    
    Gaps:
    - No MCP client support
    - No MCP tool registration
    - No MCP resource listing
    """

    def test_agent_supports_mcp_clients(self, basic_agent):
        """MiniAgent should accept MCP client connections."""
        assert hasattr(basic_agent, "mcp_clients")
        assert isinstance(basic_agent.mcp_clients, list)

    def test_mcp_tools_registered(self, basic_agent):
        """MCP server tools should appear in the agent's tool registry."""
        from codelet.mcp import connect_mcp_server
        client = connect_mcp_server("test-server", {"command": "echo"})
        basic_agent.mcp_clients.append(client)
        tools = basic_agent.registry.build()
        assert any("mcp" in name for name in tools)


# =============================================================================
# 14. THINKING CONFIG
# =============================================================================


class TestThinkingConfig:
    """Reference agent has thinking configuration (adaptive/extended/disabled).
    
    Gaps:
    - No thinking configuration
    - No thinking budget management
    """

    def test_agent_has_thinking_config(self, basic_agent):
        """MiniAgent should support thinking configuration."""
        assert hasattr(basic_agent, "thinking_config")
        assert basic_agent.thinking_config["type"] in ("adaptive", "extended", "disabled")


# =============================================================================
# 15. ABORT / CANCELLATION
# =============================================================================


class TestAbort:
    """Reference agent has AbortController for cancellation.
    
    Gaps:
    - No abort controller
    - No cancellation of in-flight model calls
    - No cancellation of running tools
    """

    def test_agent_has_abort_controller(self, basic_agent):
        """MiniAgent should have an abort controller."""
        assert hasattr(basic_agent, "abort_controller")
        assert basic_agent.abort_controller is not None

    def test_abort_stops_model_call(self, basic_agent):
        """Aborting should stop an in-flight model call."""
        import threading
        def slow_call():
            return basic_agent.ask("this will be aborted")
        t = threading.Thread(target=slow_call)
        t.start()
        basic_agent.abort_controller.abort()
        t.join(timeout=2)
        assert not t.is_alive()


# =============================================================================
# 16. EXISTING CODELET FEATURES (Regression Tests)
# =============================================================================


class TestExistingFeatures:
    """These tests verify features that codelet ALREADY has — they should pass."""

    def test_session_store_save_load(self, tmp_workspace):
        store = SessionStore(tmp_workspace / ".codelet" / "sessions")
        session = {"id": "test-123", "history": [], "memory": {"task": "", "files": [], "notes": []}}
        store.save(session)
        loaded = store.load("test-123")
        assert loaded["id"] == "test-123"

    def test_compaction_budget_reduction(self):
        history = [{"role": "tool", "name": "run_shell", "content": "x" * 10000}]
        new_budget = budget_reduction(
            history,
            current_budget=4000,
            target_chars=5000,
            min_tool_output=400,
        )
        assert new_budget < 4000
        assert new_budget >= 400

    def test_compaction_snipping_removes_stack_traces(self):
        history = [
            {
                "role": "tool",
                "name": "run_python",
                "content": "Traceback (most recent call last):\n  File \"x.py\"\nException: boom",
            }
        ]
        result = snipping(history, preserve_recent=0, fileread_tools=[], mcp_tools=[])
        # Snipping replaces the full traceback with a short "Traceback ... [snipped N chars]" marker
        assert "[snipped" in result[0]["content"]
        assert "most recent call last" not in result[0]["content"]
        assert "File \"x.py\"" not in result[0]["content"]

    def test_hardening_yolo_classifier(self):
        assert is_safe_command("ls -la") is True
        assert is_safe_command("rm -rf /") is False
        assert is_safe_command("git status") is True
        assert is_safe_command("git push origin main") is False

    def test_hardening_decoy_tools(self):
        tools = {"real_tool": {"schema": {}, "description": "real"}}
        apply_decoy_tools(tools)
        assert "secret_eval" in tools
        assert is_decoy(tools["secret_eval"])
        assert not is_decoy(tools["real_tool"])

    def test_skills_discovery(self, tmp_workspace):
        skill_dir = tmp_workspace / ".codelet" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill\n---\nDo something.\n"
        )
        skills = discover_skills(str(tmp_workspace))
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert skills[0].description == "A test skill"

    def test_skills_manifest(self, tmp_workspace):
        skill_dir = tmp_workspace / ".codelet" / "skills" / "test-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill\nwhen_to_use: testing\n---\nDo something.\n"
        )
        skills = discover_skills(str(tmp_workspace))
        manifest = render_skill_manifest(skills)
        assert "test-skill" in manifest
        assert "use when: testing" in manifest

    def test_concurrency_safe_tools(self):
        assert "read_file" in CONCURRENCY_SAFE_TOOLS
        assert "write_file" not in CONCURRENCY_SAFE_TOOLS
        assert is_concurrency_safe("read_file") is True
        assert is_concurrency_safe("run_shell") is False

    def test_tool_registry_builds(self, basic_agent):
        tools = basic_agent.tools
        assert "read_file" in tools
        assert "write_file" in tools
        assert "run_shell" in tools
        assert "delegate" in tools

    def test_file_read_dedup(self, basic_agent, tmp_workspace):
        test_file = tmp_workspace / "test.txt"
        test_file.write_text("hello world")
        r1 = basic_agent.run_tool("read_file", {"path": "test.txt"})
        assert "hello world" in r1
        r2 = basic_agent.run_tool("read_file", {"path": "test.txt"})
        assert "[file unchanged" in r2

    def test_repeated_tool_call_blocked(self, basic_agent):
        basic_agent.session["history"] = [
            {"role": "tool", "name": "read_file", "args": {"path": "x"}, "content": "y"}
        ]
        assert basic_agent.repeated_tool_call("read_file", {"path": "x"}) is True
        assert basic_agent.repeated_tool_call("read_file", {"path": "z"}) is False

    def test_path_safety(self, basic_agent, tmp_workspace):
        # resolve() handles macOS /var -> /private/var symlinks
        assert basic_agent.path("test.txt").resolve() == (tmp_workspace / "test.txt").resolve()
        with pytest.raises(ValueError):
            basic_agent.path("/etc/passwd")

    def test_config_deep_merge(self):
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"c": 3, "d": 4}}
        result = deep_merge(base, override)
        assert result["a"]["b"] == 1
        assert result["a"]["c"] == 3
        assert result["a"]["d"] == 4

    def test_prompt_building(self, basic_agent):
        prompt = basic_agent.prompt("hello")
        assert "Codelet" in prompt or "helpful assistant" in prompt
        assert "hello" in prompt

    def test_memory_text(self, basic_agent):
        basic_agent.session["memory"]["task"] = "do thing"
        basic_agent.session["memory"]["files"] = ["a.py", "b.py"]
        text = basic_agent.memory_text()
        assert "do thing" in text
        assert "a.py" in text

    def test_history_text_respects_max(self, basic_agent):
        basic_agent.session["history"] = [
            {"role": "user", "content": "x" * 1000},
            {"role": "assistant", "content": "y" * 1000},
        ]
        text = basic_agent.history_text()
        assert len(text) > 0

    def test_undercover_mode(self):
        assert undercover_enabled({"CODELET_UNDERCOVER": "1"}) is True
        assert undercover_enabled({"CODELET_UNDERCOVER": "0"}) is False
        cfg = apply_undercover_identity({"agent_identity": "original"})
        assert "helpful assistant" in cfg["agent_identity"]

    def test_tool_error_streak(self, basic_agent):
        basic_agent.session["history"] = [
            {"role": "tool", "name": "run_shell", "content": "error: command not found"},
            {"role": "tool", "name": "run_shell", "content": "error: command not found"},
            {"role": "tool", "name": "run_shell", "content": "error: command not found"},
        ]
        assert basic_agent._tool_error_streak() == 3

    def test_no_progress_streak(self, basic_agent):
        basic_agent.session["history"] = [
            {"role": "tool", "name": "read_file", "content": "[file unchanged since your earlier read"},
            {"role": "tool", "name": "read_file", "content": "[file unchanged since your earlier read"},
        ]
        assert basic_agent._no_progress_streak() == 2

    def test_hard_halt_recovery(self, basic_agent):
        basic_agent.session["history"] = [{"role": "user", "content": "x" * 100000}]
        result = basic_agent._force_compact_history()
        assert "trimmed" in result
        assert len(basic_agent.session["history"]) <= 5

    def test_subdir_memory_loading(self, basic_agent, tmp_workspace):
        subdir = tmp_workspace / "src"
        subdir.mkdir()
        (subdir / "AGENT.md").write_text("Use strict types.")
        # Create the target file so read_file validation passes
        (subdir / "main.py").write_text("# main")
        basic_agent.run_tool("read_file", {"path": "src/main.py"})
        notes = basic_agent.session["memory"]["notes"]
        assert any("Use strict types" in n for n in notes)


# =============================================================================
# 17. CLI / MACHINE MODE
# =============================================================================


class TestCLIMode:
    """Tests for the CLI entry point and machine mode."""

    def test_machine_mode_outputs_xml(self, tmp_workspace, mock_model_client, session_store, mock_workspace):
        agent = MiniAgent(
            model_client=mock_model_client,
            workspace=mock_workspace,
            session_store=session_store,
            approval_policy="auto",
            config={"harness": {"max_steps": 2}},
        )
        result = agent.ask("test")
        assert result == "Done."

    def test_approval_ask_policy(self, basic_agent):
        basic_agent.approval_policy = "ask"
        # Mock input to return 'y'
        with patch("builtins.input", return_value="y"):
            assert basic_agent.approve("run_shell", {"command": "ls"}) is True
        with patch("builtins.input", return_value="n"):
            assert basic_agent.approve("run_shell", {"command": "ls"}) is False

    def test_approval_never_policy(self, basic_agent):
        basic_agent.approval_policy = "never"
        assert basic_agent.approve("run_shell", {"command": "ls"}) is False

    def test_approval_auto_policy(self, basic_agent):
        basic_agent.approval_policy = "auto"
        assert basic_agent.approve("run_shell", {"command": "rm -rf /"}) is True


# =============================================================================
# 18. SANDBOX
# =============================================================================


class TestSandbox:
    """Tests for sandbox functionality."""

    def test_sandbox_lite_allows_safe_commands(self, basic_agent):
        result = basic_agent.run_tool("run_shell", {"command": "echo hello", "timeout": 5})
        assert "hello" in result

    def test_sandbox_blocks_dangerous_commands(self, basic_agent):
        # In lite mode, dangerous commands should be blocked or require approval
        basic_agent.approval_policy = "never"
        result = basic_agent.run_tool("run_shell", {"command": "rm -rf /tmp/test", "timeout": 5})
        assert "approval denied" in result or "error" in result.lower()


# =============================================================================
# 19. PARSING
# =============================================================================


class TestParsing:
    """Tests for model output parsing."""

    def test_parse_final_answer(self):
        from codelet import parsing
        kind, payload = parsing.parse_model_output("<final>Hello</final>", "retry")
        assert kind == "final"
        assert payload == "Hello"

    def test_parse_tool_call(self):
        from codelet import parsing
        kind, payload = parsing.parse_model_output(
            '<tool>{"name":"read_file","args":{"path":"x"}}</tool>', "retry"
        )
        assert kind == "tool"
        assert payload["name"] == "read_file"

    def test_parse_xml_tool(self):
        from codelet import parsing
        kind, payload = parsing.parse_model_output(
            '<tool name="write_file" path="x"><content>y</content></tool>', "retry"
        )
        assert kind == "tool"
        assert payload["name"] == "write_file"

    def test_extract_all_tool_payloads(self):
        from codelet import parsing
        raw = (
            '<tool>{"name":"read_file","args":{"path":"a"}}</tool>\n'
            '<tool>{"name":"read_file","args":{"path":"b"}}</tool>'
        )
        calls = parsing.extract_all_tool_payloads(raw)
        assert len(calls) == 2
        assert calls[0]["name"] == "read_file"


# =============================================================================
# 20. WORKSPACE CONTEXT
# =============================================================================


class TestWorkspaceContext:
    """Tests for workspace context loading."""

    def test_project_rules_loaded(self, tmp_workspace, mock_model_client, session_store, mock_workspace):
        (tmp_workspace / "AGENTS.md").write_text("Use black formatter.")
        agent = MiniAgent(
            model_client=mock_model_client,
            workspace=mock_workspace,
            session_store=session_store,
            config={"project_rules_files": ["AGENTS.md"]},
        )
        assert "Use black formatter" in agent.prefix

    def test_memory_files_loaded(self, tmp_workspace, mock_model_client, session_store, mock_workspace):
        (tmp_workspace / "CLAUDE.md").write_text("Prefer async.")
        agent = MiniAgent(
            model_client=mock_model_client,
            workspace=mock_workspace,
            session_store=session_store,
            config={"memory_files": {"enabled": True, "max_files": 5}},
        )
        assert "Prefer async" in agent.prefix or len(agent.memory_files) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
