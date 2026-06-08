# Codexlet ↔ OpenAI Codex Alignment Plan

## Reference: OpenAI Codex Features

Codex is a multi-surface coding agent: CLI (terminal TUI), Desktop App, IDE integration, and Web. Key UX features:

1. **Real-time streaming** — Model output streams live; tool calls execute and show results immediately
2. **Tool call cards** — Each tool call is a visual card with icon, name, args (expandable), and result
3. **Thinking/reasoning display** — Model's reasoning process shown inline or collapsible
4. **File diff viewer** — Code changes rendered as GitHub-style unified diffs with syntax highlighting
5. **Cost/usage tracking** — Per-session token usage and cost displayed in status bar
6. **Session resume** — Resume previous sessions from disk (codelet session files)
7. **Narrative view** — Semantic timeline view as alternative to raw execution logs
8. **Plan/Build mode** — Explicit toggle between planning and execution modes
9. **TODO panel** — Agent-managed TODO list, visible and updatable during session
10. **Approval UI** — Inline approval dialogs for risky tools with accept/deny/edit options
11. **Git integration** — Branch name, commit status, diff preview in status bar
12. **AGENTS.md hierarchy** — Load global + repo + cwd instruction chain
13. **Background tasks** — Spawn/kill background shell tasks with output streaming
14. **MCP management** — MCP server connection status and tool registration
15. **Undo** — `/undo` command to revert last patch
16. **Pinned composer** — Input box stays at bottom while transcript scrolls above
17. **Pasting** — Proper multiline text paste without auto-submit
18. **Skills panel** — Browse and manage repo-local and system skills
19. **Sandbox indicator** — Visual sandbox mode indicator (lite/off)
20. **Model selector** — Switch between models (GPT-4o, o3, etc.)

## Current Codexlet State

Codexlet has:
- Express server with session CRUD APIs
- HTML/CSS/TS frontend with sidebar, chat view, welcome screen
- Streaming SSE chat endpoint (`/api/sessions/:id/chat/stream`)
- Approval modal, settings panel, status bar, context menus
- Project tree, session list, mode toggle (Agent/Swarm)
- Access level dropdown (Full/Read/Ask)

## Gaps (Prioritized)

### P0 — Critical UX Gaps
1. **Real-time streaming display** — Currently waits for `<final>`; should show streaming tokens
2. **Tool call cards** — Raw text output; need visual tool cards with expand/collapse
3. **File diff viewer** — No diff visualization for write_file/patch_file
4. **Thinking display** — No reasoning/thinking process visualization
5. **Cost/usage in status bar** — Status bar exists but no cost data from codelet

### P1 — Important Feature Gaps
6. **Session resume from codelet** — Can create sessions but not resume codelet's JSON sessions
7. **Narrative view toggle** — No semantic narrative layer
8. **Plan/Build mode** — Mode toggle exists but not wired to codelet behavior
9. **TODO panel** — No agent-managed TODO list
10. **Git integration** — Status bar has git placeholder but no real data
11. **Skills browser** — Skills nav item exists but no actual skill browser
12. **Background tasks panel** — No task monitoring UI

### P2 — Polish Gaps
13. **Undo button** — No undo/revert capability in UI
14. **Model selector** — No model switching in UI
15. **MCP status** — No MCP connection indicator
16. **Pasting** — Textarea may have paste issues
17. **AGENTS.md loading** — No AGENTS.md hierarchy support
18. **Windows native** — Server uses `python3` hardcoded

## Implementation Strategy

### Stage 1: Server Enhancements (codelet CLI integration)
- Add streaming tool execution to codelet's `--machine` mode
- Add cost/usage JSON output to machine mode
- Add session resume support
- Add git status endpoint
- Add skills list endpoint
- Add background task endpoints

### Stage 2: Frontend Visualization
- Real-time streaming message rendering
- Tool call card component
- File diff viewer component
- Thinking display component
- Cost/usage display in status bar
- Narrative view toggle
- TODO panel
- Plan/Build mode wiring

### Stage 3: Integration & Polish
- Session resume from codelet sessions
- Git integration
- Skills browser
- Background tasks panel
- Undo button
- Model selector
- MCP status

## Test Strategy

Create `tests/test_codexlet_alignment.py` with tests for each gap.
Run with pytest, iterate until all pass.

## Iteration Budget

20 iterations total:
- Iterations 1–5: Server enhancements + streaming
- Iterations 6–10: Frontend visualization components
- Iterations 11–15: Integration wiring
- Iterations 16–20: Polish, edge cases, final verification
