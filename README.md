&nbsp;
# Codelet

This folder contains a small standalone coding agent:

- code: `codelet/` (package)
- CLI: `codelet`

It is a minimal local agent loop with:

- workspace snapshot collection
- stable prompt plus turn state
- structured tools (read, write, shell, Python, delegation, skills)
- approval handling for risky tools, with optional YOLO auto-approval for safe commands
- transcript and memory persistence
- bounded delegation and parallel sub-agent dispatch
- **graduated compaction cascade** for context-window pressure
- **hierarchical filesystem memory** (`CLAUDE.md` / `AGENTS.md` / `.claude/rules/`)
- **progressive-disclosure skills** (`SKILL.md` library under `.codelet/skills/`)
- **session-baseline verification** to detect repo drift across runs
- **`.env`-based configuration** for provider, key, model, and harness knobs
- **protocol integrations** — MCP client/server, A2A HTTP server, ACP stub
- **hardening countermeasures** — decoy tools, YOLO command classifier, undercover identity

The agent supports two model backends: **Ollama** (default, local) and any **OpenAI-compatible API**. The OpenAI backend ships with convenience presets for popular custom LLM providers (**Kimi/Moonshot**, **GLM/Zhipu**, **SiliconFlow**, **DeepSeek**, **OpenRouter**, **Together**, **DashScope**, ...) so you can pick one with a single `--provider` flag.

<a href="https://magazine.sebastianraschka.com/p/components-of-a-coding-agent">
  <img src="https://substack-post-media.s3.amazonaws.com/public/images/49b97718-57f4-4977-99c8-8ad5c4d32af3_1548x862.png" width="500px">
</a>

<br>

**[The detailed tutorial: Components of a Coding Agent](https://magazine.sebastianraschka.com/p/components-of-a-coding-agent)**


&nbsp;
## Six Core Components

<a href="https://magazine.sebastianraschka.com/p/components-of-a-coding-agent">
  <img alt="Six core components of a coding agent" src="https://sebastianraschka.com/images/github/mini-coding-agent/six-components.webp" width="500px">
</a>

This coding harness is organized around six practical building blocks:

1. **Live repo context**  
   The agent collects stable workspace facts upfront, such as repo layout, instructions, and git state.
2. **Prompt shape and cache reuse**  
   A stable prompt prefix, which is separate from the changing request, transcript, and memory so repeated model calls can reuse the static parts efficiently.
3. **Structured tools, validation, and permissions**  
   The model works through named tools with checked inputs, workspace path validation, and approval gates instead of free-form arbitrary actions. File edits are safe by default: `delete_file` moves to trash and `patch_file` operates on exact text blocks.
4. **Context reduction and output management**  
   Long outputs are clipped, repeated reads are deduplicated, and older transcript entries are compressed to keep prompt size under control.
5. **Transcripts, memory, and resumption**  
   The runtime keeps both a full durable transcript and a smaller working memory so sessions can be resumed while preserving important state via working memory.
6. **Delegation and bounded subagents**  
   Scoped subtasks can be delegated to helper agents that inherit enough context to help (but operate within limits). Parallel dispatch (`delegate_parallel`) and task decomposition (`decompose`) enable multi-agent workflows.

&nbsp;
## Requirements

You need:

- Python 3.10+
- One of the supported backends:
  - **Ollama** (default): Ollama installed locally with a model pulled
  - **OpenAI-compatible API**: an API key for OpenAI or a compatible service

Optional:

- `uv` for environment management and the `codelet` CLI entry point
- `openai` Python package when using the OpenAI backend (`pip install openai`)

This project has no mandatory Python runtime dependency beyond the standard library for the Ollama backend, so you can run it directly with `python -m codelet` if you do not want to use `uv`. (PyYAML is optional — install it only if you want to override the packaged YAML config; otherwise the built-in defaults are used.)

&nbsp;
## Install Ollama

Install Ollama on your machine so the `ollama` command is available in your shell.

Official installation link: [ollama.com/download](https://ollama.com/download)

Then verify:

```bash
ollama --help
```

Start the server:

```bash
ollama serve
```

In another terminal, pull a model. Example:

```bash
ollama pull qwen3.5:4b
```

Qwen 3.5 model library:

- [ollama.com/library/qwen3.5](https://ollama.com/library/qwen3.5)

The default in this project is `qwen3.5:4b`. If you have sufficient memory, it is worth trying a larger model such as `qwen3.5:9b` or another larger Qwen 3.5 variant. The agent just sends prompts to Ollama's `/api/generate` endpoint.

&nbsp;
## Project Setup

Clone the repo or your fork and change into it:

```bash
git clone https://github.com/xhyccc/codelet-agent.git codelet
cd codelet
```

If you forked it first, use your fork URL instead:

```bash
git clone https://github.com/<your-github-user>/codelet-agent.git codelet
cd codelet
```



&nbsp;
## Basic Usage

### Ollama backend (default)

Start the agent:

```bash
cd codelet
uv run codelet
```

Without `uv`, run the script directly:

```bash
cd codelet
python -m codelet
```

By default it uses:

- backend: `ollama`
- model: `qwen3.5:4b`
- approval: `ask`

### OpenAI-compatible backend

```bash
uv run codelet --backend openai --model gpt-4o-mini --openai-api-key YOUR_KEY
```

Or set the key via environment variable:

```bash
export OPENAI_API_KEY=YOUR_KEY
uv run codelet --backend openai --model gpt-4o-mini
```

To use a compatible third-party API (e.g., a local OpenAI-compatible server):

```bash
uv run codelet --backend openai --model your-model \
  --openai-base-url http://localhost:8000/v1 --openai-api-key none
```

### Custom LLM API providers (Kimi, GLM, SiliconFlow, ...)

For convenience, the agent ships with presets for popular OpenAI-compatible
LLM API providers. Pick one with `--provider` and the agent will fill in the
correct base URL, default model, and the conventional API key environment
variable for you. The OpenAI client wrapper is reused under the hood.

| `--provider` | Provider | Default model | API key env var |
|---|---|---|---|
| `openai` | OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `kimi` / `moonshot` | Moonshot AI (Kimi) | `moonshot-v1-8k` | `MOONSHOT_API_KEY` |
| `glm` / `zhipu` | Zhipu AI (GLM) | `glm-4-flash` | `ZHIPU_API_KEY` |
| `siliconflow` | SiliconFlow | `Qwen/Qwen2.5-7B-Instruct` | `SILICONFLOW_API_KEY` |
| `deepseek` | DeepSeek | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| `openrouter` | OpenRouter | `openai/gpt-4o-mini` | `OPENROUTER_API_KEY` |
| `together` | Together AI | `meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo` | `TOGETHER_API_KEY` |
| `dashscope` | Alibaba DashScope (Qwen) | `qwen-plus` | `DASHSCOPE_API_KEY` |
| `custom` | Any other OpenAI-compatible endpoint | (must pass `--model`) | `CUSTOM_LLM_API_KEY` (or `OPENAI_API_KEY`) |

Examples:

```bash
# Kimi / Moonshot
export MOONSHOT_API_KEY=sk-...
uv run codelet --provider kimi "List the files in this repo."

# Zhipu GLM
export ZHIPU_API_KEY=sk-...
uv run codelet --provider glm --model glm-4-plus "Summarize README.md"

# SiliconFlow
export SILICONFLOW_API_KEY=sk-...
uv run codelet --provider siliconflow

# Any other OpenAI-compatible endpoint
export CUSTOM_LLM_API_KEY=sk-...
uv run codelet --provider custom \
  --openai-base-url https://my-internal-llm.example/v1 \
  --model my-internal-model
```

`--model`, `--openai-base-url`, and `--openai-api-key` always take precedence
over the preset, so you can target a specific model (e.g. `moonshot-v1-32k`)
or override the endpoint as needed.

### One-shot (non-interactive) mode

Pass a task as a positional argument to run the agent non-interactively and exit:

```bash
uv run codelet "List the files in this repo and summarize the project."
```

Approval defaults to `auto` in one-shot mode. To override:

```bash
uv run codelet --approval ask "Refactor the main function."
```

For a concrete usage example, see [EXAMPLE.md](EXAMPLE.md).

&nbsp;
## Approval Modes

Risky tools such as shell commands and file writes are gated by approval.

- `--approval ask`
  prompts before risky actions (default and recommended)
- `--approval auto`
  allows risky actions automatically, including arbitrary command execution and file writes by the model; use only with trusted prompts and trusted repositories
- `--approval never`
  denies risky actions

Example:

```bash
uv run codelet --approval auto
```



&nbsp;
## Lightweight Sandboxing

Risky tools (`run_shell` and `run_python`) run with best-effort, lightweight
sandboxing by default. This is not a true security boundary — always combine
with `--approval ask` and run untrusted prompts in a real VM / container —
but it makes accidental damage and obvious prompt-injection attacks harder.

When `--sandbox lite` (the default) is active, the agent:

- **Blocks obviously destructive command patterns** before they ever reach the
  shell: `sudo`, `rm -rf /`, `mkfs`, `dd of=/dev/...`, `shutdown` / `reboot`,
  `curl ... | bash`, fork bombs, `chmod 777 /`, writes to raw disk devices,
  and similar patterns.
- **Blocks dangerous Python idioms** in `run_python`, such as
  `shutil.rmtree('/etc')`, `open('/dev/sda', 'wb')`, or shelling out to `sudo`.
- **Strips sensitive environment variables** (anything matching `*_API_KEY`,
  `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `AWS_*`, `AZURE_*`, `GCP_*`,
  `GITHUB_TOKEN`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `PYTHONPATH`)
  from subprocesses started by the agent.
- **Applies POSIX resource limits** to subprocesses where supported:
  CPU time (`RLIMIT_CPU` = 30s), virtual memory (`RLIMIT_AS` = 1 GiB), and
  maximum file size (`RLIMIT_FSIZE` = 64 MiB).

Disable the sandbox with `--sandbox off` if you need it out of the way (for
example, when running CI scripts that legitimately need access to secrets in
the environment).

```bash
# default — lightweight sandbox enabled
uv run codelet

# disable
uv run codelet --sandbox off
```



&nbsp;
## Resume Sessions

The agent saves sessions under the target workspace root in:

```text
.codelet/sessions/
```

Resume the latest session:

```bash
uv run codelet --resume latest
```


Resume a specific session:

```bash
uv run codelet --resume 20260401-144025-2dd0aa
```


&nbsp;
## Interactive Commands

Inside the REPL, slash commands are handled directly by the agent instead of
being sent to the model as a normal task.

- `/help`
  shows the list of available interactive commands
- `/memory`
  prints the distilled session memory, including the current task, tracked files, and notes
- `/session`
  prints the path to the current saved session JSON file
- `/reset`
  clears the current session history and distilled memory but keeps you in the REPL
- `/exit`
  exits the interactive session
- `/quit`
  exits the interactive session; alias for `/exit`

&nbsp;
## Main CLI Flags

```bash
uv run codelet --help
```

Without `uv`:

```bash
python -m codelet --help
```

CLI flags are passed before the agent starts. Use them to choose the workspace,
model backend, resume behavior, approval mode, allowed tool categories, and generation limits.

Important flags:

- `--cwd`
  sets the workspace directory the agent should inspect and modify; default: `.`
- `--backend`
  selects the model backend: `ollama` or `openai`; default: `ollama`
- `--provider`
  convenience preset for a custom OpenAI-compatible LLM API (`openai`, `kimi`/`moonshot`, `glm`/`zhipu`, `siliconflow`, `deepseek`, `openrouter`, `together`, `dashscope`, or `custom`); sets `--backend openai`, the appropriate `--openai-base-url`, and reads the API key from the provider's conventional env var (e.g. `MOONSHOT_API_KEY` for `kimi`)
- `--model`
  selects the model name, such as `qwen3.5:4b` for Ollama or `gpt-4o-mini` for OpenAI; default: `qwen3.5:4b`
- `--host`
  points the agent at the Ollama server URL (usually not needed); default: `http://127.0.0.1:11434`
- `--ollama-timeout`
  controls how long the client waits for an Ollama response (usually not needed); default: `300` seconds
- `--openai-api-key`
  API key for the OpenAI backend; falls back to the `OPENAI_API_KEY` environment variable
- `--openai-base-url`
  base URL for any OpenAI-compatible API; default: `https://api.openai.com/v1`
- `--openai-timeout`
  request timeout for the OpenAI backend; default: `60` seconds
- `--resume`
  resumes a saved session by id or uses `latest`; default: start a new session
- `--approval`
  controls how risky tools are handled: `ask`, `auto`, or `never`; defaults to `auto` for one-shot prompts, `ask` for interactive mode
- `--allow`
  restricts which tool categories are available: `read`, `write`, `bash`, `python`; defaults to all categories
- `--sandbox`
  lightweight sandboxing for `run_shell` / `run_python`: `lite` (default; blocks destructive command patterns, strips sensitive env vars, applies POSIX resource limits) or `off`
- `--max-steps`
  limits how many model and tool turns are allowed for one user request; default: `6`
- `--max-new-tokens`
  caps the model output length for each step; default: `512`
- `--temperature`
  controls sampling randomness; default: `0.2`
- `--top-p`
  controls nucleus sampling for generation; default: `0.9`
- `--config`
  path to a YAML file that overrides any subset of the packaged prompt/harness defaults
- `--env-file`
  path to a `.env` file with LLM provider/key/model and harness overrides; defaults to auto-discovery of `<workspace>/.env`. See [Configuring from `.env`](#configuring-from-env).
- `--no-welcome`
  suppress the startup banner (useful for scripting or piped output)
- `--decoy-tools`
  inject decoy tool entries (`secret_eval`, `network_probe`, `exfiltrate`) into the prompt surface; calls are refused at runtime. See [Hardening](#hardening).
- `--yolo`
  auto-approve obviously-safe shell commands (`ls`, `pwd`, `cat`, `git status`, ...) when `--approval ask` is active, without prompting. See [Hardening](#hardening).
- `--undercover`
  replace the agent identity with a generic "helpful assistant" string and suppress the welcome banner; equivalent to `CODELET_UNDERCOVER=1`. See [Hardening](#hardening).

&nbsp;
## Configuration via YAML

All prompts (agent identity, rules, examples, retry notices, coordinator/override layers) and harness parameters (`max_steps`, `max_new_tokens`, timeouts, sampling, `allowed_ops`, `sandbox`, `approval`, sandbox denylists) are loaded from `codelet/config/default.yaml`. You can override any subset of them without touching the packaged file:

1. Pass `--config path/to/your.yaml` on the CLI, or
2. Drop a `.codelet/config.yaml` file at the root of your workspace - it is auto-discovered.

Resolution order is: packaged defaults < workspace `.codelet/config.yaml` < explicit `--config` file. Per-call CLI flags still take precedence over everything in YAML.

Project-specific rules placed in `AGENTS.md` (or `.codelet/rules.md`) at the repo root are automatically pulled into the prompt's `<project-rules>` layer.

PyYAML is an optional dependency. Install with `pip install pyyaml` (or `pip install -e .[yaml]`) only if you want to load custom YAML; otherwise the agent uses its built-in defaults.

### Prompt architecture

The prompt is assembled in up to six XML-tagged layers ordered from most stable (top, cacheable) to most volatile (bottom):

1. `<agent-identity>` — who the agent is. **Always present.** Replaced by a generic string when `--undercover` / `CODELET_UNDERCOVER=1` is active.
2. `<system-defaults>` — immutable rules, tool catalog, response examples. **Always present.**
3. `<project-rules>` — per-repo overrides (`AGENTS.md`, `.codelet/rules.md`), selected hierarchical memory files, and the skill manifest (name + description for each discovered skill). Emitted only when content is found.
4. `<coordinator>` — delegation/swarm guidance. Emitted only when delegation is enabled.
5. `<override>` — volatile session overrides from YAML config or CLI. Emitted only when configured.
6. `<workspace>` — workspace snapshot (cwd, branch, status, docs). **Always present.**

Then per-turn volatile state is appended in `<memory>`, `<transcript>`, `<request>` tags. This shape mirrors the "Immutable Prompt Principle" so KV caches can be reused across turns.

&nbsp;
## Available Tools

The agent exposes the following tools to the model. Tools are grouped into categories that can be restricted with `--allow`.

| Tool | Category | Risky | Description |
|---|---|---|---|
| `list_files` | `read` | no | List files in the workspace |
| `read_file` | `read` | no | Read a UTF-8 file by line range |
| `search` | `read` | no | Search the workspace with `rg` or a simple fallback |
| `glob` | `read` | no | List workspace files matching a glob pattern (e.g. `**/*.py`) |
| `write_file` | `write` | **yes** | Write a text file |
| `patch_file` | `write` | **yes** | Replace one exact text block in a file (in-place diff-style edit) |
| `delete_file` | `write` | **yes** | Delete a file (moves to `.codelet/trash/` instead of permanent deletion) |
| `move_file` | `write` | **yes** | Move or rename a file inside the workspace |
| `run_shell` | `bash` | **yes** | Run a shell command in the repo root (PowerShell/cmd on Windows) |
| `run_python` | `python` | **yes** | Execute Python code in the repo root and return its output |
| `delegate` | — | no | Ask a bounded read-only child agent to investigate |
| `delegate_parallel` | — | no | Dispatch multiple sub-agent tasks concurrently and collect results |
| `decompose` | — | no | Ask the agent to split a complex task into independent subtasks |
| `load_skill` | — | no | Fetch the full body of a named skill from `.codelet/skills/` |
| `remember_fact` | `write` | no | Append a short fact to `.codelet/repo-memory.md` |

Risky tools require approval. The approval mode (`--approval`) controls whether the user is prompted (`ask`), the action is allowed automatically (`auto`), or it is always denied (`never`).

&nbsp;
## Progressive-Disclosure Skills

Skills are reusable prompt snippets stored under `.codelet/skills/<name>/SKILL.md`. At startup, only the name and one-line description of each skill are injected into the system prompt. The model calls `load_skill(name)` to retrieve the full body on demand — keeping the baseline prompt small while making complex procedures available.

A skill directory looks like:

```
.codelet/skills/
  changelog-writer/
    SKILL.md           ← required; YAML front-matter + body
    template.md        ← optional sibling assets
```

`SKILL.md` minimal structure:

```markdown
---
name: changelog-writer
description: Generate a CHANGELOG.md entry from recent git history.
---
## Instructions
Use `git log --oneline` to list recent commits ...
```

If no front-matter is present the directory name is used as `name` and the
first non-blank line is used as `description`.

&nbsp;
## Parallel Delegation

Two tools handle multi-agent workloads:

- **`decompose`** — the agent announces that it is splitting a complex task into a list of independent subtasks. The host records them and the agent works through them sequentially.
- **`delegate_parallel`** — dispatch up to `harness.delegate_parallel_max_workers` (default: `4`) sub-agent tasks concurrently in a thread pool. Each task runs a fresh child agent that inherits the parent workspace; results are collected and returned as JSON.

Both tools respect the depth guard (`harness.max_depth`, default `1`): sub-agents cannot themselves spawn further parallel agents, preventing runaway recursion.

&nbsp;
## Protocol Integrations

The `codelet.protocols` subpackage provides lightweight, stdlib-only adapters so the agent can talk to the wider LLM ecosystem. All three modules can be imported independently.

### MCP — Model Context Protocol

```python
from codelet.protocols import MCPClient, register_mcp_tools, serve_mcp_stdio

# Consume tools from an external MCP server
client = MCPClient(command=["npx", "-y", "@some/mcp-server"])
client.start()
# Tools are registered as  mcp__<server>__<tool>  in the agent's registry

# Expose the agent's own tools to an MCP host
serve_mcp_stdio(agent)  # communicates over stdin/stdout
```

Server discovery uses `~/.codelet/mcp.json` or `<workspace>/.codelet/mcp.json`:

```json
{
  "servers": {
    "my-tools": {
      "command": ["python", "-m", "my_mcp_server"],
      "env": {},
      "timeout": 10.0
    }
  }
}
```

Registered MCP tools appear in the prompt as `mcp__<server>__<tool>` and behave like native tools (approval, timeout, output clipping all apply).

### A2A — Agent-to-Agent

```python
from codelet.protocols import serve_a2a_blocking, build_agent_card

# Serve the agent over HTTP so other agents can call it
serve_a2a_blocking(agent, host="0.0.0.0", port=8080)
```

- `GET /.well-known/agent.json` — returns the agent card (name, description, capabilities)
- `POST /tasks/send` `{"id": "...", "message": "..."}` — runs a one-shot task and returns `{"id": "...", "status": "completed", "result": "..."}`

### ACP — Agent Communication Protocol

`ACPSessionStub` provides the message-shape dataclass for experimentation. The full protocol is not bundled; swap `protocols/acp.py` for the upstream reference implementation when needed.

&nbsp;
## Hardening

Three optional countermeasures are available via CLI flags or config, inspired by patterns observed in production coding agents.

### Decoy tools (`--decoy-tools`)

Inject three fake tool entries (`secret_eval`, `network_probe`, `exfiltrate`) into the system prompt. Any call to a decoy is refused at runtime with a safety-policy message. The goal is to make prompt-extraction and distillation attacks harder: an attacker reading the prompt sees a tool surface that does not actually work.

Enable via flag:
```bash
uv run codelet --decoy-tools
```

Or via config:
```yaml
harness:
  decoy_tools: true
```

### YOLO command classifier (`--yolo`)

When `--approval ask` is active, the agent normally prompts before every `run_shell` call. With `--yolo`, trivially safe commands (`ls`, `pwd`, `cat`, `git status`, `git log`, `python --version`, ...) are auto-approved without a prompt. Commands containing shell metacharacters (`;`, `|`, `&`, `` ` ``, `$`, `>`, `<`, `\`) or anything outside the explicit safelist are still gated normally.

```bash
uv run codelet --yolo
```

Or via config:
```yaml
harness:
  yolo_classifier: true
```

### Undercover identity (`--undercover`)

Replace the agent's `<agent-identity>` layer with a generic "helpful assistant" string and suppress the welcome banner. Equivalent to setting `CODELET_UNDERCOVER=1` in the environment. Useful for benchmark and eval runs where you do not want the model to recognise the harness.

```bash
uv run codelet --undercover
# or
CODELET_UNDERCOVER=1 uv run codelet
```

&nbsp;
## Example

See [EXAMPLE.md](EXAMPLE.md)

&nbsp;
## Notes & Tips

- The agent expects the model to emit either `<tool>...</tool>` or `<final>...</final>`.
- Different models will follow those instructions with different reliability; use a stronger instruction-following model if the format is not respected.
- The agent is intentionally small and optimized for readability, not robustness.
- Use `--allow read` to restrict the agent to read-only access to the workspace.
- Sessions are saved automatically; use `--resume latest` to continue where you left off.
- Tool output (`run_shell`, `run_python`) is printed to the terminal immediately after each approved call, before the model formulates its final answer.
- Use `--no-welcome` to suppress the startup banner (handy when piping output or scripting).

&nbsp;
## Configuring from `.env`

The CLI auto-discovers a `.env` file at the workspace root (override the location with `--env-file PATH`). Values from `.env` populate the LLM provider, key, model, base URL, and a small set of harness knobs.

Resolution order is: **CLI flags > `.env` > workspace `.codelet/config.yaml` > packaged YAML defaults**. `.env` values are also exported into `os.environ` (non-clobbering by default) so existing API-key resolution logic continues to work.

Supported keys:

| Key | Effect |
|---|---|
| `LLM_PROVIDER` | Same vocabulary as `--provider`: `kimi`, `moonshot`, `zhipu`/`glm`, `siliconflow`, `deepseek`, `openrouter`, `together`, `dashscope`, `openai`, `custom` |
| `LLM_API_KEY` | Generic API key; takes precedence over all provider-specific keys below |
| `LLM_MODEL` | Same as `--model` |
| `LLM_BASE_URL` | Same as `--openai-base-url` (required for `custom`) |
| `KIMI_API_KEY` / `MOONSHOT_API_KEY` | Aliases for the Moonshot/Kimi key |
| `ZHIPU_API_KEY` | Zhipu / GLM |
| `SILICONFLOW_API_KEY` | SiliconFlow |
| `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `TOGETHER_API_KEY`, `DASHSCOPE_API_KEY`, `OPENAI_API_KEY` | Provider-specific fallbacks |
| `CODELET_MAX_STEPS` | Sets `harness.max_steps` (CLI default) |
| `CODELET_MAX_NEW_TOKENS` | Sets `harness.max_new_tokens` |
| `CODELET_OPENAI_TIMEOUT` | Sets `harness.openai_timeout` |
| `CODELET_TOOL_TIMEOUT` | Default timeout (seconds) for `run_shell` / `run_python` tool calls; model may request less but not more than `CODELET_TOOL_MAX_TIMEOUT`; default `20` |
| `CODELET_TOOL_MAX_TIMEOUT` | Upper clamp (seconds) on tool call timeouts the model may request; default `120` |
| `CODELET_CMD` | Recognised by the documented launcher schema; ignored by the agent itself |

Example `.env`:

```bash
LLM_PROVIDER=kimi
KIMI_API_KEY=sk-xxxxxxxxxxxxxxxx
LLM_MODEL=moonshot-v1-32k
CODELET_MAX_STEPS=15
CODELET_OPENAI_TIMEOUT=300
CODELET_MAX_NEW_TOKENS=8192
CODELET_TOOL_TIMEOUT=60
CODELET_TOOL_MAX_TIMEOUT=300
```

See [`.env.example`](.env.example) for the full template. `.env` is git-ignored by default.

&nbsp;
## Context Management & Compaction

When the transcript would otherwise overflow the model's context window, the agent runs a **graduated compaction cascade** rather than simply dropping old messages (which severely degrades reasoning). The cascade is implemented in [`codelet/compaction.py`](codelet/compaction.py).

Five stages run in order; the cascade stops at the first stage that brings the rendered transcript under `harness.compaction.target_chars`:

1. **Budget reduction** — dynamically trim `max_tool_output` to create a soft ceiling against context flooding.
2. **Snipping** — mathematically excise high-volume, low-value strings (Python tracebacks, banner-style shell noise) from older items.
3. **Microcompaction** — clear intermediate outputs from iterative tool calls. **MCP outputs are preserved verbatim** (schema stability) and **`FileRead` results skip budgeting entirely** so the model retains full visibility of critical code.
4. **Context collapse** — read-time projection that flattens older history non-destructively (one-line role/name summary per item).
5. **Auto-compaction** — secondary LLM call with the `autocompact` system prompt. Explicitly preserves definitive user directives, actionable task items, and architectural notes while highly summarizing the verbose operational history.

If a single execution loop refills the context immediately after auto-compaction (token thrashing), a **`HardHaltError`** is raised and surfaced to the user instead of looping forever.

Tune the cascade under `harness.compaction` in YAML or in your override config:

```yaml
harness:
  compaction:
    target_chars: 12000       # soft ceiling on the rendered transcript
    min_tool_output: 400      # floor for the dynamic tool-output budget
    microcompact_clip: 120    # per-item clip during stage 3
    preserve_recent: 4        # tail always kept verbatim
    thrash_min_relief: 0.1    # raise HardHaltError if relief < 10%
    mcp_tools: [delegate]     # preserved verbatim
    fileread_tools: [read_file]  # skipped from budgeting
    auto_compaction: true
    autocompact_tokens: 512
```

The durable on-disk transcript (`.codelet/sessions/*.json`) is **never** mutated by compaction — only the prompt-rendered view is. Resuming a session keeps full fidelity.

&nbsp;
## Hierarchical Filesystem Memory

The agent treats context windows as finite, volatile, and quickly degrading. Instead of using a vector database, it offloads cognitive burden onto **markdown protocol files** at well-known paths, governed by a multi-level memory structure:

| Layer | Paths (in resolution order) | Purpose |
|---|---|---|
| **global** | `/etc/codelet/CLAUDE.md` and any `*.md` in `/etc/codelet/` | System-wide defaults |
| **user**   | `~/.claude/`, `~/.codelet/` (`*.md`) | User preferences |
| **project**| `<repo>/.claude/rules/*.md`, `<repo>/.codelet/rules.md`, `<repo>/AGENTS.md`, `<repo>/CLAUDE.md` | Project-specific architectural records |
| **local**  | `<repo>/CLAUDE.local.md` | Git-ignored workspace notes |

The agent does an **LLM-friendly header-based scan** (no vector embeddings) of each candidate, scores them against the active task description, and includes up to `memory_files.max_files` (default **5**) in the `<project-rules>` layer. Layer precedence (`local > project > user > global`) breaks ties.

Configure under `memory_files`:

```yaml
memory_files:
  enabled: true
  max_files: 5
  # Optional path overrides; see codelet.memory_files for defaults:
  # global_roots: [/etc/codelet]
  # user_roots:   [~/.claude, ~/.codelet]
  # project_paths: [.claude/rules, .codelet/rules.md, AGENTS.md, CLAUDE.md]
  # local_paths:   [CLAUDE.local.md]
```

`CLAUDE.local.md` is git-ignored by default so it never leaks into commits.

&nbsp;
## Session Baselines

Every agent session begins with a **verification baseline check** against the physical repository state to prevent compounding hallucinations and architectural drift across multiple, disjointed agent runs. The baseline records:

- workspace root
- current branch and `HEAD` commit
- short digest of `git status --porcelain`
- size + truncated sha256 of each watched memory file

The baseline is persisted into the session JSON. On the next session, `MiniAgent.baseline_drift` exposes a human-readable list of differences (`"HEAD moved: aaaa -> bbbb"`, `"file changed on disk: AGENTS.md"`, ...). Workspace rules in `CLAUDE.md` / `AGENTS.md` guarantee probabilistic compliance better than any external retrieval system would.

See [`codelet/baseline.py`](codelet/baseline.py) for the API.
