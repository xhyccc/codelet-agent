&nbsp;
# Codelet

A minimal standalone coding agent aligned with OpenAI Codex UX. Runs in your terminal with real-time streaming, tool call cards, and session management.

**Key features:**
- **Real-time streaming** — token-by-token output with live tool call cards
- **Plan / Build modes** — Plan mode drafts a step-by-step plan; Build mode executes directly
- **Session management** — Create, resume, fork, rename sessions with persistent history
- **Cost tracking** — Per-session cost, token usage, and budget limits
- **Permission system** — Granular rules: alwaysAllow / alwaysDeny / alwaysAsk per tool type
- **Skills library** — Progressive-disclosure `SKILL.md` files under `.codelet/skills/`
- **Context compaction** — Graduated cascade (snipping → microcompaction → auto-compaction) prevents context overflow
- **Hierarchical memory** — `AGENTS.md` / `CLAUDE.md` / `.codelet/rules.md` layered project memory
- **Sandboxing** — Lightweight sandbox for shell/Python: blocks destructive patterns, strips secrets, applies POSIX limits
- **Hardening** — Decoy tools, YOLO safe-command classifier, undercover identity mode
- **Protocol integrations** — MCP client/server, A2A HTTP server, ACP stub

**Backends:** Ollama (default, local) or any OpenAI-compatible API (OpenAI, Kimi/Moonshot, GLM/Zhipu, SiliconFlow, DeepSeek, OpenRouter, Together, DashScope, ...).

&nbsp;
## Quick Start

### Prerequisites
- Python 3.10+
- Ollama (for local backend) or an API key (for cloud backend)

### Install

```bash
git clone https://github.com/xhyccc/codelet-agent.git
cd codelet-agent
pip install -e .[openai,yaml]   # or just `pip install -e .` for Ollama only
```

### Run

**Ollama (local):**
```bash
ollama serve                    # in one terminal
ollama pull qwen3.5:4b          # pull a model
codelet                         # start the agent
```

**OpenAI-compatible API:**
```bash
export OPENAI_API_KEY=sk-...
codelet --backend openai --model gpt-4o-mini
```

**Provider presets (Kimi, GLM, SiliconFlow, ...):**
```bash
export MOONSHOT_API_KEY=sk-...
codelet --provider kimi
```

**One-shot mode:**
```bash
codelet "List the files and summarize the project"
```

**Resume session:**
```bash
codelet --resume latest
```

&nbsp;
## Build Standalone Binary

```bash
pip install pyinstaller
python3 scripts/build_binary.py
# Output: dist/codelet (12 MB, no Python required)
```

&nbsp;
## CLI Flags

| Flag | Description |
|---|---|
| `--cwd` | Workspace directory (default: `.`) |
| `--backend` | `ollama` or `openai` |
| `--provider` | Preset: `kimi`, `glm`, `siliconflow`, `deepseek`, `openrouter`, `together`, `dashscope`, `openai`, `custom` |
| `--model` | Model name |
| `--openai-api-key` / `--openai-base-url` | OpenAI backend credentials |
| `--resume` | Resume session by ID or `latest` |
| `--approval` | `ask` (default interactive), `auto` (default one-shot), `never` |
| `--allow` | Restrict tools: `read`, `write`, `bash`, `python` |
| `--sandbox` | `lite` (default) or `off` |
| `--max-steps` | Max tool/model turns per request (default: 6) |
| `--max-new-tokens` | Max output tokens per step (default: 512) |
| `--config` | YAML config override |
| `--env-file` | `.env` file with provider/key/model settings |
| `--machine` | Machine-readable XML output (`<tool>` / `<final>`) |
| `--decoy-tools` | Inject fake tools to harden against distillation |
| `--yolo` | Auto-approve safe commands (`ls`, `cat`, `git status`, ...) |
| `--undercover` | Generic identity, suppress welcome banner |

Full list: `codelet --help`

&nbsp;
## Configuration

**`.env` file** (auto-discovered at workspace root):
```bash
LLM_PROVIDER=kimi
MOONSHOT_API_KEY=sk-...
LLM_MODEL=moonshot-v1-32k
CODELET_MAX_STEPS=15
CODELET_MAX_NEW_TOKENS=8192
```

**YAML config** (`.codelet/config.yaml` or `--config`):
```yaml
harness:
  max_steps: 10
  max_new_tokens: 2048
  approval: ask
  sandbox: lite
  compaction:
    target_chars: 12000
    auto_compaction: true
```

Resolution: CLI flags > `.env` > `.codelet/config.yaml` > packaged defaults.

&nbsp;
## Interactive Commands

Inside the REPL:
- `/help` — list commands
- `/memory` — show distilled session memory
- `/session` — show session file path
- `/reset` — clear history, keep REPL
- `/exit` or `/quit` — exit

&nbsp;
## Tools

| Tool | Category | Risky | Description |
|---|---|---|---|
| `list_files`, `read_file`, `search`, `glob` | `read` | no | Inspect workspace |
| `write_file`, `patch_file`, `delete_file`, `move_file` | `write` | **yes** | Modify files |
| `run_shell`, `run_python` | `bash` / `python` | **yes** | Execute commands (sandboxed) |
| `delegate`, `delegate_parallel`, `decompose` | — | no | Multi-agent workflows |
| `load_skill` | — | no | Load a skill from `.codelet/skills/` |
| `remember_fact` | `write` | no | Append to `.codelet/repo-memory.md` |

&nbsp;
## Skills

Reusable prompt snippets stored under `.codelet/skills/<name>/SKILL.md`. Only name + description are injected at startup; the model calls `load_skill(name)` to fetch the full body on demand.

```markdown
---
name: changelog-writer
description: Generate a CHANGELOG entry from git history.
---
Use `git log --oneline` to list recent commits ...
```

&nbsp;
## Architecture

The prompt is assembled in six XML-tagged layers (stable → volatile):
1. `<agent-identity>` — who the agent is
2. `<system-defaults>` — rules, tool catalog, examples
3. `<project-rules>` — `AGENTS.md`, `.codelet/rules.md`, memory files, skill manifest
4. `<coordinator>` — delegation/swarm guidance
5. `<override>` — session overrides from config/CLI
6. `<workspace>` — repo snapshot (cwd, branch, status)

Per-turn: `<memory>`, `<transcript>`, `<request>`.

**Context compaction** (5-stage cascade when approaching window limit):
1. Budget reduction → 2. Snipping → 3. Microcompaction → 4. Context collapse → 5. Auto-compaction (secondary LLM call). Durable session JSON is never mutated.

**Hierarchical memory** (layered markdown files, no vector DB):
- global: `/etc/codelet/CLAUDE.md`
- user: `~/.claude/`, `~/.codelet/`
- project: `<repo>/.claude/rules/*.md`, `AGENTS.md`, `CLAUDE.md`
- local: `CLAUDE.local.md` (git-ignored)

**Session baselines** — verify repo state across runs to detect drift (branch moves, file changes, HEAD shifts).

&nbsp;
## Hardening

- **Decoy tools** (`--decoy-tools`): inject fake tools (`secret_eval`, `network_probe`, `exfiltrate`) that are refused at runtime
- **YOLO classifier** (`--yolo`): auto-approve safe commands (`ls`, `pwd`, `cat`, `git status`) when `--approval ask`
- **Undercover** (`--undercover`): generic identity, suppress welcome banner

&nbsp;
## Example

See [EXAMPLE.md](EXAMPLE.md)

&nbsp;
## License

MIT
