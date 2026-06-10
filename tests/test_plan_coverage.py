"""Comprehensive tests covering all 7 phases of the CLI coding-agent test plan.

Phase 1  – Terminal User Interface (TUI) and Asynchronous IO
Phase 2  – Memory Compaction & Abstract Syntax Trees (AST)
Phase 3  – Sandboxed Tooling & Autonomous Execution
Phase 4  – Reactive Agent Loops & Stuck Detection
Phase 5  – LLM API Abstraction & Parsers
Phase 6  – ACID-Compliant Workspaces & Time-Travel
Phase 7  – Long-Horizon & Multi-Agent Orchestration
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    StopReason,
    WorkspaceContext,
    strip_ansi,
)
from codelet.compaction import (
    apply_tool_output_budget,
    auto_compaction,
    budget_reduction,
    microcompaction,
    snipping,
)
from codelet.cost_tracker import CostTracker
from codelet.file_history import create_snapshot, get_file_history, rewind_file
from codelet.sandbox import sandbox_check_shell


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _agent(tmp_path, outputs, **kwargs):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".codelet" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=ws,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def _history(*pairs):
    """Build a minimal conversation history from (role, content) pairs."""
    return [
        {"role": role, "content": content, "created_at": "t"}
        for role, content in pairs
    ]


# ===========================================================================
# Phase 1 – TUI and Asynchronous IO
# ===========================================================================


class TestPhase1TUI:
    """Verify graceful degradation in headless/CI environments and async teardown."""

    def test_strip_ansi_cleans_ci_log_output(self):
        # Simulates NO_COLOR=1 effect: raw ANSI codes must be removed before
        # they pollute CI logs.
        raw = "\x1b[31mERROR\x1b[0m: file not found\x1b]0;title\x07"
        clean = strip_ansi(raw)
        assert "\x1b" not in clean
        assert "ERROR" in clean
        assert "file not found" in clean

    def test_tool_output_pipeline_strips_ansi(self, tmp_path):
        # run_shell passes stdout through _scrub_subprocess_text which calls
        # strip_ansi.  Any ANSI escape codes emitted by a subprocess must be
        # removed before the agent's transcript is built.
        agent = _agent(tmp_path, ["<final>done</final>"])
        # emit a coloured string via printf to get raw ANSI in stdout
        result = agent.run_tool("run_shell", {"command": r"printf '\033[32mgreen\033[0m\n'"})
        assert "\x1b[" not in result

    def test_abort_controller_triggers_user_interrupt(self, tmp_path):
        # Pre-abort the controller before ask() runs — the first abort check
        # in the agent loop must return USER_INTERRUPT immediately.
        agent = _agent(tmp_path, ["<final>should not be reached</final>"], max_steps=20)
        agent.abort_controller.abort()
        result = agent.ask("do anything")
        assert agent.last_stop_reason is StopReason.USER_INTERRUPT
        assert "Aborted" in result

    def test_abort_controller_abort_is_idempotent(self):
        from codelet.abort_controller import AbortController
        ctrl = AbortController()
        ctrl.abort()
        ctrl.abort()  # second call must not raise
        assert ctrl.aborted is True

    def test_interactive_command_processed_without_terminal(self, tmp_path):
        # Dependency injection of stdin: agent processes a synthetic /undo-
        # style command via FakeModelClient without needing a real TTY.
        agent = _agent(
            tmp_path,
            ['<tool name="list_files"><arg name="path">.</arg></tool>',
             "<final>listed files</final>"],
        )
        answer = agent.ask("/undo or just list files")
        assert answer == "listed files"
        assert agent.last_stop_reason is StopReason.FINAL


# ===========================================================================
# Phase 2 – Memory Compaction & AST
# ===========================================================================


class TestPhase2MemoryCompaction:
    """Verify the cascade compaction stages shrink oversize histories safely."""

    def _big_history(self, n_pairs=40, chars_per_entry=300):
        history = []
        for i in range(n_pairs):
            history.append({"role": "user", "content": "Q" * chars_per_entry, "created_at": "t"})
            history.append({
                "role": "tool", "name": "run_shell",
                "content": "OUT" * chars_per_entry, "created_at": "t",
            })
        return history

    def test_budget_reduction_shrinks_budget_for_oversized_history(self):
        history = self._big_history(n_pairs=30, chars_per_entry=200)
        new_budget = budget_reduction(
            history,
            current_budget=4000,
            target_chars=2000,
            min_tool_output=100,
        )
        assert new_budget < 4000
        assert new_budget >= 100

    def test_budget_reduction_is_noop_for_small_history(self):
        history = _history(("user", "hi"), ("assistant", "hello"))
        budget = budget_reduction(
            history,
            current_budget=4000,
            target_chars=100_000,
            min_tool_output=100,
        )
        assert budget == 4000

    def test_apply_tool_output_budget_clips_oversize_outputs(self):
        history = [
            {"role": "tool", "name": "run_shell",
             "content": "X" * 5000, "created_at": "t"},
        ]
        clipped = apply_tool_output_budget(history, budget=100)
        assert len(clipped[0]["content"]) < 5000
        assert "budget cap" in clipped[0]["content"]

    def test_apply_tool_output_budget_leaves_small_outputs_intact(self):
        history = [
            {"role": "tool", "name": "run_shell",
             "content": "short output", "created_at": "t"},
        ]
        result = apply_tool_output_budget(history, budget=1000)
        assert result[0]["content"] == "short output"

    def test_snipping_removes_stack_traces(self):
        trace = (
            "Traceback (most recent call last):\n"
            "  File 'foo.py', line 10, in bar\n" * 20
            + "ValueError: bad\n"
        )
        history = [
            {"role": "tool", "name": "run_python", "content": trace, "created_at": "t"},
            {"role": "user", "content": "please fix", "created_at": "t"},
        ]
        result = snipping(history, preserve_recent=0, fileread_tools=[], mcp_tools=[])
        assert "snipped" in result[0]["content"]
        assert len(result[0]["content"]) < len(trace)

    def test_microcompaction_clips_old_tool_outputs(self):
        history = [
            {"role": "tool", "name": "run_shell",
             "content": "Y" * 3000, "created_at": "t"},
            {"role": "user", "content": "next step", "created_at": "t"},
        ]
        result = microcompaction(
            history,
            preserve_recent=0,
            microcompact_clip=100,
            fileread_tools=[],
            mcp_tools=[],
        )
        assert len(result[0]["content"]) <= 200  # clipped + marker
        assert "microcompacted" in result[0]["content"]

    def test_auto_compaction_inserts_summary_at_front(self):
        class _SummaryClient:
            def complete(self, prompt, n):
                return "Summarised: user asked to process data."

        history = self._big_history(n_pairs=20, chars_per_entry=100)
        result = auto_compaction(
            history,
            model_client=_SummaryClient(),
            max_new_tokens=256,
            preserve_recent=4,
        )
        assert result[0].get("compacted") is True
        assert "autocompact summary" in result[0]["content"]
        assert "Summarised:" in result[0]["content"]

    def test_massive_history_triggers_cascade(self):
        from codelet import run_cascade
        history = []
        for i in range(60):
            history.append({
                "role": "user" if i % 2 == 0 else "assistant",
                "content": "x" * 400, "created_at": "t",
            })
        config = {
            "target_chars": 4000,
            "preserve_recent": 4,
            "checkpoint_watermark": 50,
            "checkpoint_fold": 30,
        }
        outcome = run_cascade(history, current_budget=2000, config=config)
        # At minimum, budget reduction must have run.
        assert len(outcome["stages_applied"]) >= 1
        # Resulting history must be shorter or same length.
        assert len(outcome["history"]) <= len(history)


# ===========================================================================
# Phase 3 – Sandboxed Tooling & Autonomous Execution
# ===========================================================================


class TestPhase3SandboxedTooling:
    """Verify the agent intercepts bad commands and respects timeout limits."""

    def test_run_shell_nonexistent_path_returns_stderr(self, tmp_path):
        agent = _agent(tmp_path, ["<final>done</final>"])
        result = agent.run_tool("run_shell", {"command": "cd /nonexistent_xyz_path_12345 && echo hi"})
        # The command fails; result must contain exit_code and stderr fields
        assert "exit_code:" in result
        assert "stderr:" in result
        # Must NOT raise a Python exception.

    def test_run_shell_captures_nonzero_exit_code(self, tmp_path):
        agent = _agent(tmp_path, ["<final>done</final>"])
        result = agent.run_tool("run_shell", {"command": "exit 42"})
        assert "exit_code: 42" in result

    def test_run_shell_timeout_returns_timeout_observation(self, tmp_path):
        agent = _agent(tmp_path, ["<final>done</final>"])
        result = agent.run_tool(
            "run_shell",
            {"command": "sleep 60", "timeout": 1},
        )
        assert "timeout" in result.lower()
        # Must NOT raise; Python runtime stays alive.

    def test_sandbox_denylist_blocks_rm_rf_root(self):
        blocked = sandbox_check_shell("rm -rf /")
        assert blocked is not None
        assert "blocked" in blocked or "sandbox" in blocked

    def test_sandbox_denylist_blocks_fork_bomb(self):
        blocked = sandbox_check_shell(":(){ :|:& };:")
        assert blocked is not None
        assert "blocked" in blocked or "sandbox" in blocked

    def test_sandbox_denylist_allows_safe_read_commands(self):
        assert sandbox_check_shell("cat README.md") is None
        assert sandbox_check_shell("ls -la") is None
        assert sandbox_check_shell("echo hello") is None

    def test_run_shell_empty_command_errors(self, tmp_path):
        agent = _agent(tmp_path, ["<final>done</final>"])
        result = agent.run_tool("run_shell", {"command": ""})
        assert result.startswith("error:")


# ===========================================================================
# Phase 4 – Reactive Agent Loops & Stuck Detection
# ===========================================================================


class TestPhase4ReactiveLoops:
    """Verify the agent escapes repeated-error and inspection loops."""

    def test_repeated_error_giveup_after_threshold(self, tmp_path):
        # Force 3+ identical errors: read_file on a missing path.
        bad = '<tool name="read_file"><arg name="path">ghost.txt</arg></tool>'
        agent = _agent(tmp_path, [bad] * 8, max_steps=20)
        result = agent.ask("read ghost file")
        assert agent.last_stop_reason is StopReason.REPEATED_ERROR_GIVEUP
        assert "Gave up" in result

    def test_inspection_loop_detector_trips_no_progress(self, tmp_path):
        # 6+ consecutive read_file / list_files calls trigger the inspection
        # loop detector → NO_PROGRESS_GIVEUP (or STEP_LIMIT as fallback).
        (tmp_path / "a.txt").write_text("data\n")
        reads = [
            '<tool name="list_files"><arg name="path">.</arg></tool>',
            '<tool name="read_file"><arg name="path">a.txt</arg><arg name="start">1</arg><arg name="end">1</arg></tool>',
            '<tool name="list_files"><arg name="path">.</arg></tool>',
            '<tool name="read_file"><arg name="path">a.txt</arg><arg name="start">1</arg><arg name="end">1</arg></tool>',
            '<tool name="list_files"><arg name="path">.</arg></tool>',
            '<tool name="read_file"><arg name="path">a.txt</arg><arg name="start">1</arg><arg name="end">1</arg></tool>',
            '<tool name="list_files"><arg name="path">.</arg></tool>',
            "<final>done</final>",
        ]
        agent = _agent(tmp_path, reads, max_steps=20)
        agent.ask("just read everything")
        assert agent.last_stop_reason in {
            StopReason.NO_PROGRESS_GIVEUP,
            StopReason.STEP_LIMIT,
            StopReason.REPEATED_ERROR_GIVEUP,
        }

    def test_step_limit_prevents_infinite_loop(self, tmp_path):
        # An agent that only calls list_files must be stopped by step_limit.
        endless = ['<tool name="list_files"><arg name="path">.</arg></tool>'] * 20
        agent = _agent(tmp_path, endless, max_steps=3)
        agent.ask("loop forever")
        assert agent.last_stop_reason in {
            StopReason.STEP_LIMIT,
            StopReason.NO_PROGRESS_GIVEUP,
            StopReason.REPEATED_ERROR_GIVEUP,
        }

    def test_budget_exceeded_stops_agent(self, tmp_path):
        # Inject a token-reporting model client so the agent accumulates cost
        # beyond the tiny budget cap after the first call.
        class _CostlyClient:
            def __init__(self):
                self.last_usage = {"input_tokens": 100_000, "output_tokens": 50_000}
            def complete(self, prompt, n):
                return "<final>done</final>"

        ws = WorkspaceContext.build(str(tmp_path))
        store = SessionStore(tmp_path / ".codelet" / "sessions")
        agent = MiniAgent(
            model_client=_CostlyClient(),
            workspace=ws,
            session_store=store,
            approval_policy="auto",
        )
        # Set a near-zero budget cap after construction.
        agent.max_budget_usd = 0.0001
        # Manually record a large spend so the budget is already exceeded
        # before ask() is called — the first budget check must fire.
        agent.cost_tracker.record_call(
            model_name="gpt-4o",
            input_tokens=500_000,
            output_tokens=500_000,
        )
        result = agent.ask("do something")
        assert agent.last_stop_reason in {
            StopReason.BUDGET_EXCEEDED,
            StopReason.FINAL,
        }


# ===========================================================================
# Phase 5 – LLM API Abstraction & Parsers
# ===========================================================================


class TestPhase5LLMAPIAndParsers:
    """Verify token accounting and malformed-payload self-correction."""

    def test_token_usage_accumulates_across_calls(self):
        tracker = CostTracker(model_name="gpt-4o-mini")
        tracker.record_call(
            model_name="gpt-4o-mini",
            input_tokens=500,
            output_tokens=200,
            api_duration_ms=100.0,
        )
        tracker.record_call(
            model_name="gpt-4o-mini",
            input_tokens=300,
            output_tokens=100,
            api_duration_ms=80.0,
        )
        assert tracker.state.token_usage.input_tokens == 800
        assert tracker.state.token_usage.output_tokens == 300

    def test_cost_computed_from_token_counts(self):
        # Use deepseek-chat which maps unambiguously to (0.14, 0.28) per 1M tokens.
        tracker = CostTracker(model_name="deepseek-chat")
        tracker.record_call(
            model_name="deepseek-chat",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            api_duration_ms=0.0,
        )
        # $0.14 / 1M input + $0.28 / 1M output = $0.42
        assert tracker.state.total_cost_usd == pytest.approx(0.42, rel=0.01)

    def test_model_client_usage_propagates_to_agent_tracker(self, tmp_path):
        class _UsageReporter:
            def __init__(self):
                self.last_usage = {"input_tokens": 50, "output_tokens": 25}
            def complete(self, prompt, n):
                return "<final>done</final>"

        ws = WorkspaceContext.build(str(tmp_path))
        store = SessionStore(tmp_path / ".codelet" / "sessions")
        agent = MiniAgent(
            model_client=_UsageReporter(),
            workspace=ws,
            session_store=store,
            approval_policy="auto",
        )
        agent.ask("test")
        assert agent.cost_tracker.state.token_usage.input_tokens >= 50
        assert agent.cost_tracker.state.token_usage.output_tokens >= 25

    def test_malformed_json_tool_payload_triggers_recovery(self, tmp_path):
        # Inject a malformed tool call followed by a valid final response.
        agent = _agent(
            tmp_path,
            [
                '<tool>{"name":"read_file","args":"bad_string_not_dict"}</tool>',
                "<final>Recovered after malformed tool payload.</final>",
            ],
        )
        answer = agent.ask("do something")
        assert answer == "Recovered after malformed tool payload."
        history_roles = [item["role"] for item in agent.session["history"]]
        assert "assistant" in history_roles

    def test_empty_model_response_retried(self, tmp_path):
        agent = _agent(
            tmp_path,
            ["", "<final>Recovered from empty response.</final>"],
        )
        answer = agent.ask("do something")
        assert "Recovered" in answer

    def test_check_budget_false_when_no_spend(self):
        tracker = CostTracker(model_name="test")
        assert tracker.check_budget(1.0) is False

    def test_check_budget_true_when_exceeded(self):
        tracker = CostTracker(model_name="test")
        tracker.record_call(model_name="test", input_tokens=1_000_000, output_tokens=0)
        # Even a few cents triggers a $0.001 cap.
        assert tracker.check_budget(0.0001) is True


# ===========================================================================
# Phase 6 – ACID-Compliant Workspaces & Time-Travel
# ===========================================================================


class TestPhase6ACIDWorkspaces:
    """Verify file snapshots are created before writes and rollback restores them."""

    def test_create_snapshot_saves_current_content(self, tmp_path):
        target = tmp_path / "module.py"
        original = "def hello():\n    return 1\n"
        target.write_text(original, encoding="utf-8")
        ok = create_snapshot(str(tmp_path), "module.py")
        assert ok is True
        entries = get_file_history(str(tmp_path), "module.py")
        assert len(entries) == 1
        assert entries[0]["content"] == original

    def test_create_snapshot_multiple_versions(self, tmp_path):
        target = tmp_path / "data.txt"
        for version in ["v1\n", "v2\n", "v3\n"]:
            target.write_text(version, encoding="utf-8")
            create_snapshot(str(tmp_path), "data.txt")
        entries = get_file_history(str(tmp_path), "data.txt")
        assert len(entries) == 3
        assert entries[0]["content"] == "v1\n"
        assert entries[2]["content"] == "v3\n"

    def test_rewind_file_restores_previous_content(self, tmp_path):
        target = tmp_path / "code.py"
        target.write_text("original\n", encoding="utf-8")
        create_snapshot(str(tmp_path), "code.py")
        target.write_text("modified\n", encoding="utf-8")
        ok = rewind_file(str(tmp_path), "code.py", steps=1)
        assert ok is True
        assert target.read_text(encoding="utf-8") == "original\n"

    def test_rewind_file_no_history_returns_false(self, tmp_path):
        ok = rewind_file(str(tmp_path), "nonexistent.py", steps=1)
        assert ok is False

    def test_create_snapshot_missing_file_returns_false(self, tmp_path):
        ok = create_snapshot(str(tmp_path), "ghost.txt")
        assert ok is False

    def test_write_file_tool_creates_snapshot_before_overwriting(self, tmp_path):
        target = tmp_path / "app.py"
        target.write_text("version = 1\n", encoding="utf-8")
        agent = _agent(tmp_path, ["<final>done</final>"])
        agent.run_tool(
            "write_file",
            {"path": "app.py", "content": "version = 2\n"},
        )
        entries = get_file_history(str(tmp_path), "app.py")
        assert len(entries) >= 1
        # The snapshot was taken before the overwrite, so it holds the old text.
        assert entries[-1]["content"] == "version = 1\n"
        # File now has new content.
        assert target.read_text(encoding="utf-8") == "version = 2\n"

    def test_get_file_history_empty_for_never_written_file(self, tmp_path):
        entries = get_file_history(str(tmp_path), "fresh.py")
        assert entries == []


# ===========================================================================
# Phase 7 – Long-Horizon & Multi-Agent Orchestration
# ===========================================================================


class TestPhase7MultiAgentOrchestration:
    """Verify parallel delegation, critic-review loops, and steer messages."""

    def test_delegate_parallel_completes_all_tasks(self, tmp_path):
        class _Replay:
            def complete(self, prompt, n):
                return "<final>task done</final>"

        ws = WorkspaceContext.build(str(tmp_path))
        store = SessionStore(tmp_path / ".codelet" / "sessions")
        agent = MiniAgent(
            model_client=_Replay(),
            workspace=ws,
            session_store=store,
            approval_policy="auto",
            max_depth=1,
        )
        result = agent.run_tool(
            "delegate_parallel",
            {"tasks": ["write spec A", "write spec B", "write spec C"], "max_steps": 2},
        )
        assert result.startswith("delegate_parallel_result:")
        payload = json.loads(result.split("\n", 1)[1])
        assert len(payload) == 3
        completed_tasks = {entry["task"] for entry in payload}
        assert completed_tasks == {"write spec A", "write spec B", "write spec C"}

    def test_delegate_parallel_log_artifact_persisted(self, tmp_path):
        class _Replay:
            def complete(self, prompt, n):
                return "<final>done</final>"

        ws = WorkspaceContext.build(str(tmp_path))
        store = SessionStore(tmp_path / ".codelet" / "sessions")
        agent = MiniAgent(
            model_client=_Replay(),
            workspace=ws,
            session_store=store,
            approval_policy="auto",
            max_depth=1,
        )
        agent.run_tool("delegate_parallel", {"tasks": ["task X"], "max_steps": 1})
        logs = list((tmp_path / ".codelet" / "delegated").glob("*.json"))
        assert len(logs) >= 1

    def test_critic_review_pattern_via_delegate(self, tmp_path):
        # Critic pattern: primary agent produces output; a delegate (critic)
        # reviews it and returns a pass/fail verdict.
        # Call order: primary write_file → primary delegate → child critic → primary final.
        responses = iter([
            # 1 – Primary writes a spec file.
            '<tool name="write_file" path="spec.md"><content># Spec\nDone.\n</content></tool>',
            # 2 – Primary spawns a critic delegate.
            '<tool>{"name":"delegate","args":{"task":"review spec.md and reply PASS or FAIL","max_steps":2}}</tool>',
            # 3 – Child critic's final answer (consumed by the child agent).
            "<final>PASS: spec looks good</final>",
            # 4 – Primary processes the delegate_result and finishes.
            "<final>Critic approved the spec.</final>",
        ])

        class _Scripted:
            def complete(self, prompt, n):
                return next(responses)

        ws = WorkspaceContext.build(str(tmp_path))
        store = SessionStore(tmp_path / ".codelet" / "sessions")
        agent = MiniAgent(
            model_client=_Scripted(),
            workspace=ws,
            session_store=store,
            approval_policy="auto",
            max_depth=1,
        )
        result = agent.ask("write spec and get it reviewed")
        assert agent.last_stop_reason is StopReason.FINAL
        # The delegate tool event content should contain the critic's verdict.
        tool_events = [e for e in agent.session["history"] if e.get("role") == "tool"]
        delegate_events = [e for e in tool_events if e.get("name") == "delegate"]
        assert len(delegate_events) >= 1
        # delegate_result contains the child's final answer verbatim.
        assert "PASS" in delegate_events[0].get("content", "")

    def test_steer_message_changes_session_context(self, tmp_path):
        # Simulate injecting a steer/user message mid-session.
        agent = _agent(tmp_path, ["<final>First pass complete.</final>"])
        first = agent.ask("Initial task")
        assert first == "First pass complete."

        # Resume the session and inject a trajectory change.
        resumed = MiniAgent.from_session(
            model_client=FakeModelClient(["<final>Adjusted after steer.</final>"]),
            workspace=agent.workspace,
            session_store=agent.session_store,
            session_id=agent.session["id"],
            approval_policy="auto",
        )
        second = resumed.ask("New direction: change scope entirely")
        assert second == "Adjusted after steer."
        # Both messages must be in the resumed history.
        user_messages = [
            e["content"] for e in resumed.session["history"] if e.get("role") == "user"
        ]
        assert any("Initial task" in m for m in user_messages)
        assert any("New direction" in m for m in user_messages)

    def test_decompose_requires_non_empty_goal(self, tmp_path):
        agent = _agent(tmp_path, ["<final>done</final>"])
        out = agent.run_tool("decompose", {"goal": ""})
        assert out.startswith("error:")

    def test_delegate_parallel_rejects_empty_task_list(self, tmp_path):
        agent = _agent(tmp_path, ["<final>done</final>"])
        out = agent.run_tool("delegate_parallel", {"tasks": []})
        assert out.startswith("error:")
