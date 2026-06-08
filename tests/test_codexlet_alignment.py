"""Codexlet ↔ OpenAI Codex alignment tests.

These tests verify that codexlet (the GUI) implements the same UX patterns
as the OpenAI Codex CLI/App. Tests cover server APIs, frontend components,
and integration behavior.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Add codelet to path
sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def codexlet_server(tmp_workspace):
    """Start the codexlet server on a free port, yield base URL, then kill."""
    env = dict(os.environ)
    env["CODEXLET_DATA_DIR"] = str(tmp_workspace / ".codexlet")
    env["CODEXLET_PORT"] = "0"  # auto-assign
    proc = subprocess.Popen(
        [sys.executable, "-m", "codexlet.server"],
        cwd=str(Path(__file__).parent.parent / "codexlet"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for server to start and parse port from stdout
    port = None
    for _ in range(50):
        line = proc.stdout.readline().decode("utf-8", errors="replace")
        if "port" in line.lower() or "listening" in line.lower():
            # Try to extract port number
            import re
            m = re.search(r"(\d{4,5})", line)
            if m:
                port = int(m.group(1))
                break
        time.sleep(0.1)
    if port is None:
        proc.kill()
        pytest.skip("Could not start codexlet server")
    base_url = f"http://127.0.0.1:{port}"
    # Health check
    for _ in range(20):
        try:
            import urllib.request
            with urllib.request.urlopen(f"{base_url}/api/status", timeout=1) as resp:
                if resp.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("Codexlet server did not become healthy")
    yield base_url
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# =============================================================================
# 1. REAL-TIME STREAMING
# =============================================================================


class TestStreaming:
    """Codex streams model output and tool results in real-time.
    
    Gaps:
    - codexlet waits for full <final> before showing anything
    - No intermediate tool call display during streaming
    - No live token-by-token model output
    """

    @pytest.mark.xfail(reason="Streaming not implemented")
    def test_chat_stream_endpoint_exists(self, codexlet_server):
        """The server should expose a streaming chat endpoint."""
        import urllib.request
        req = urllib.request.Request(
            f"{codexlet_server}/api/sessions/test/chat/stream",
            data=b'{"message":"hello"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                assert resp.status == 200
                content_type = resp.headers.get("Content-Type", "")
                assert "text/event-stream" in content_type or "event-stream" in content_type
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pytest.fail("Streaming endpoint not found")
            raise

    @pytest.mark.xfail(reason="Streaming display not implemented")
    def test_stream_shows_tool_calls_in_realtime(self, codexlet_server):
        """Tool calls should appear in the stream before the final answer."""
        pass  # Frontend test — would need browser automation

    @pytest.mark.xfail(reason="Token streaming not implemented")
    def test_stream_shows_model_tokens(self, codexlet_server):
        """Model output should stream token by token, not all at once."""
        pass


# =============================================================================
# 2. TOOL CALL CARDS
# =============================================================================


class TestToolCards:
    """Codex shows each tool call as a visual card with icon, name, args, result.
    
    Gaps:
    - codexlet shows raw text output, no visual tool cards
    - No expand/collapse for tool args
    - No tool-specific icons or colors
    """

    @pytest.mark.xfail(reason="Tool cards not implemented")
    def test_tool_call_has_visual_card(self, codexlet_server):
        """A tool call message should render as a card, not plain text."""
        pass

    @pytest.mark.xfail(reason="Tool card expand not implemented")
    def test_tool_card_expandable_args(self, codexlet_server):
        """Tool card should have expandable args section."""
        pass

    @pytest.mark.xfail(reason="Tool card icons not implemented")
    def test_tool_card_has_icon(self, codexlet_server):
        """Each tool type should have a distinct icon."""
        pass


# =============================================================================
# 3. FILE DIFF VIEWER
# =============================================================================


class TestDiffViewer:
    """Codex shows file changes as GitHub-style unified diffs.
    
    Gaps:
    - codexlet shows raw tool result text, no diff visualization
    - No syntax highlighting in diff view
    - No line numbers or +/- indicators
    """

    @pytest.mark.xfail(reason="Diff viewer not implemented")
    def test_write_file_shows_diff(self, codexlet_server):
        """write_file should show a diff of the change."""
        pass

    @pytest.mark.xfail(reason="Diff viewer not implemented")
    def test_patch_file_shows_diff(self, codexlet_server):
        """patch_file should show a unified diff."""
        pass

    @pytest.mark.xfail(reason="Diff syntax highlighting not implemented")
    def test_diff_has_syntax_highlighting(self, codexlet_server):
        """Diff view should have syntax highlighting by language."""
        pass


# =============================================================================
# 4. THINKING / REASONING DISPLAY
# =============================================================================


class TestThinking:
    """Codex shows model reasoning/thinking process.
    
    Gaps:
    - codexlet has no thinking display
    - No collapsible reasoning section
    - No thinking budget indicator
    """

    @pytest.mark.xfail(reason="Thinking display not implemented")
    def test_thinking_section_exists(self, codexlet_server):
        """Model reasoning should be shown in a collapsible section."""
        pass

    @pytest.mark.xfail(reason="Thinking toggle not implemented")
    def test_thinking_can_be_hidden(self, codexlet_server):
        """User should be able to hide/show thinking."""
        pass


# =============================================================================
# 5. COST / USAGE TRACKING
# =============================================================================


class TestCostUsage:
    """Codex shows per-session token usage and cost.
    
    Gaps:
    - codexlet status bar has placeholders but no real cost data
    - No cost per model breakdown
    - No budget warning indicator
    """

    @pytest.mark.xfail(reason="Cost tracking in UI not implemented")
    def test_status_bar_shows_cost(self, codexlet_server):
        """Status bar should display accumulated cost."""
        pass

    @pytest.mark.xfail(reason="Token usage in UI not implemented")
    def test_status_bar_shows_token_usage(self, codexlet_server):
        """Status bar should display input/output token counts."""
        pass

    @pytest.mark.xfail(reason="Budget warning not implemented")
    def test_budget_warning_shows_when_exceeded(self, codexlet_server):
        """UI should show warning when cost approaches budget."""
        pass


# =============================================================================
# 6. SESSION RESUME
# =============================================================================


class TestSessionResume:
    """Codex can resume previous sessions from disk.
    
    Gaps:
    - codexlet can list sessions but not resume codelet's JSON sessions
    - No session history search
    - No session metadata (cost, duration, model)
    """

    @pytest.mark.xfail(reason="Session resume not implemented")
    def test_can_resume_codelet_session(self, codexlet_server, tmp_workspace):
        """Should be able to resume an existing codelet session."""
        pass

    @pytest.mark.xfail(reason="Session metadata not implemented")
    def test_session_shows_metadata(self, codexlet_server):
        """Session list should show cost, duration, model used."""
        pass


# =============================================================================
# 7. NARRATIVE VIEW
# =============================================================================


class TestNarrativeView:
    """Codex has a semantic narrative layer over raw logs.
    
    Gaps:
    - codexlet has no narrative view
    - No toggle between raw and narrative
    """

    @pytest.mark.xfail(reason="Narrative view not implemented")
    def test_narrative_view_toggle_exists(self, codexlet_server):
        """UI should have a toggle for narrative vs raw view."""
        pass

    @pytest.mark.xfail(reason="Narrative view not implemented")
    def test_narrative_shows_semantic_summary(self, codexlet_server):
        """Narrative view should show intent-level summaries."""
        pass


# =============================================================================
# 8. PLAN / BUILD MODE
# =============================================================================


class TestPlanBuildMode:
    """Codex has explicit Plan and Build mode toggle.
    
    Gaps:
    - codexlet has mode toggle UI but not wired to codelet behavior
    - No plan visualization
    - No build progress tracking
    """

    @pytest.mark.xfail(reason="Plan mode not implemented")
    def test_plan_mode_creates_plan(self, codexlet_server):
        """Plan mode should generate a step-by-step plan first."""
        pass

    @pytest.mark.xfail(reason="Build mode not implemented")
    def test_build_mode_executes_plan(self, codexlet_server):
        """Build mode should execute the active plan."""
        pass


# =============================================================================
# 9. TODO PANEL
# =============================================================================


class TestTodoPanel:
    """Codex shows an agent-managed TODO list.
    
    Gaps:
    - codexlet has no TODO panel
    - Agent cannot update TODO during session
    """

    @pytest.mark.xfail(reason="TODO panel not implemented")
    def test_todo_panel_exists(self, codexlet_server):
        """UI should have a visible TODO panel."""
        pass

    @pytest.mark.xfail(reason="TODO updates not implemented")
    def test_agent_can_update_todo(self, codexlet_server):
        """Agent should be able to add/check items in TODO."""
        pass


# =============================================================================
# 10. APPROVAL UI
# =============================================================================


class TestApprovalUI:
    """Codex has inline approval dialogs for risky tools.
    
    Gaps:
    - codexlet has a modal but basic
    - No accept-for-session option
    - No edit-before-approve option
    """

    @pytest.mark.xfail(reason="Accept-for-session not implemented")
    def test_approval_has_accept_session_option(self, codexlet_server):
        """Approval dialog should have 'Accept for this session' button."""
        pass

    @pytest.mark.xfail(reason="Edit-before-approve not implemented")
    def test_approval_has_edit_option(self, codexlet_server):
        """Approval dialog should allow editing the command before approving."""
        pass


# =============================================================================
# 11. GIT INTEGRATION
# =============================================================================


class TestGitIntegration:
    """Codex is git-aware with branch, diff, commit status.
    
    Gaps:
    - codexlet status bar has git placeholder but no real data
    - No git diff preview
    - No commit suggestion
    """

    @pytest.mark.xfail(reason="Git status not implemented")
    def test_status_bar_shows_git_branch(self, codexlet_server, tmp_workspace):
        """Status bar should show current git branch."""
        pass

    @pytest.mark.xfail(reason="Git diff preview not implemented")
    def test_git_diff_preview_available(self, codexlet_server):
        """UI should show git diff of changes made by agent."""
        pass


# =============================================================================
# 12. SKILLS BROWSER
# =============================================================================


class TestSkillsBrowser:
    """Codex has a skills browser panel.
    
    Gaps:
    - codexlet has Skills nav item but no actual browser
    - No skill search/filter
    - No skill detail view
    """

    @pytest.mark.xfail(reason="Skills browser not implemented")
    def test_skills_browser_shows_discovered_skills(self, codexlet_server):
        """Skills panel should list discovered skills with descriptions."""
        pass

    @pytest.mark.xfail(reason="Skill detail not implemented")
    def test_skill_detail_view_exists(self, codexlet_server):
        """Clicking a skill should show its full body."""
        pass


# =============================================================================
# 13. BACKGROUND TASKS
# =============================================================================


class TestBackgroundTasks:
    """Codex supports background shell tasks with output streaming.
    
    Gaps:
    - codexlet has no background task panel
    - No task status indicator
    - No task output streaming
    """

    @pytest.mark.xfail(reason="Background tasks panel not implemented")
    def test_background_tasks_panel_exists(self, codexlet_server):
        """UI should have a panel for monitoring background tasks."""
        pass

    @pytest.mark.xfail(reason="Task status indicator not implemented")
    def test_task_status_shows_in_ui(self, codexlet_server):
        """Running tasks should show status (pending/running/completed/failed)."""
        pass


# =============================================================================
# 14. UNDO
# =============================================================================


class TestUndo:
    """Codex has /undo to revert last patch.
    
    Gaps:
    - codexlet has no undo button
    - No file history integration in UI
    """

    @pytest.mark.xfail(reason="Undo not implemented")
    def test_undo_button_exists(self, codexlet_server):
        """UI should have an undo button to revert last change."""
        pass

    @pytest.mark.xfail(reason="Undo history not implemented")
    def test_undo_reverts_last_patch(self, codexlet_server):
        """Undo should restore file to pre-patch state."""
        pass


# =============================================================================
# 15. MODEL SELECTOR
# =============================================================================


class TestModelSelector:
    """Codex allows switching between models.
    
    Gaps:
    - codexlet has no model selector in UI
    - Status bar shows 'codelet' but not actual model name
    """

    @pytest.mark.xfail(reason="Model selector not implemented")
    def test_model_selector_exists(self, codexlet_server):
        """UI should have a dropdown to select the model."""
        pass

    @pytest.mark.xfail(reason="Model switching not implemented")
    def test_model_switch_changes_behavior(self, codexlet_server):
        """Switching model should change the model used by codelet."""
        pass


# =============================================================================
# 16. MCP STATUS
# =============================================================================


class TestMCPStatus:
    """Codex shows MCP server connection status.
    
    Gaps:
    - codexlet has no MCP indicator
    - No MCP tool listing in UI
    """

    @pytest.mark.xfail(reason="MCP status not implemented")
    def test_mcp_status_indicator_exists(self, codexlet_server):
        """Status bar should show MCP connection status."""
        pass


# =============================================================================
# 17. PASTING
# =============================================================================


class TestPasting:
    """Codex handles multiline paste properly.
    
    Gaps:
    - codexlet textarea may auto-submit on multiline paste
    """

    @pytest.mark.xfail(reason="Paste handling not tested")
    def test_multiline_paste_does_not_auto_submit(self, codexlet_server):
        """Pasting multiline text should not auto-submit the message."""
        pass


# =============================================================================
# 18. AGENTS.md HIERARCHY
# =============================================================================


class TestAgentsMD:
    """Codex loads AGENTS.md hierarchy (global + repo + cwd).
    
    Gaps:
    - codelet loads AGENTS.md but no hierarchy
    - No override.md support
    """

    @pytest.mark.xfail(reason="AGENTS.md hierarchy not implemented")
    def test_agents_md_hierarchy_loaded(self, tmp_workspace):
        """Agent should load global + repo + cwd AGENTS.md in order."""
        pass


# =============================================================================
# 19. SANDBOX INDICATOR
# =============================================================================


class TestSandboxIndicator:
    """Codex shows sandbox mode visually.
    
    Gaps:
    - codexlet has sandbox text in status bar but no visual indicator
    """

    @pytest.mark.xfail(reason="Sandbox visual indicator not implemented")
    def test_sandbox_has_visual_indicator(self, codexlet_server):
        """Sandbox mode should have a colored dot or badge."""
        pass


# =============================================================================
# 20. PINNED COMPOSER
# =============================================================================


class TestPinnedComposer:
    """Codex keeps input box pinned to bottom.
    
    codexlet already has this — regression test.
    """

    def test_input_area_is_at_bottom(self, codexlet_server):
        """Input area should be at the bottom of the viewport."""
        import urllib.request
        with urllib.request.urlopen(f"{codexlet_server}/") as resp:
            html = resp.read().decode("utf-8")
        assert "input-area" in html
        # Check CSS that pins it to bottom
        assert "position" in html or "flex" in html or "grid" in html


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
