&nbsp;
# Mini-Coding-Agent

This folder contains a small standalone coding agent:

- code: `mini_coding_agent/` (package)
- CLI: `mini-coding-agent`

It is a minimal local agent loop with:

- workspace snapshot collection
- stable prompt plus turn state
- structured tools
- approval handling for risky tools
- transcript and memory persistence
- bounded delegation

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
   The model works through named tools with checked inputs, workspace path validation, and approval gates instead of free-form arbitrary actions.
4. **Context reduction and output management**  
   Long outputs are clipped, repeated reads are deduplicated, and older transcript entries are compressed to keep prompt size under control.
5. **Transcripts, memory, and resumption**  
   The runtime keeps both a full durable transcript and a smaller working memory so sessions can be resumed while preserving important state via working memory.
6. **Delegation and bounded subagents**  
   Scoped subtasks can be delegated to helper agents that inherit enough context to help (but operate within limits).

&nbsp;
## Requirements

You need:

- Python 3.10+
- One of the supported backends:
  - **Ollama** (default): Ollama installed locally with a model pulled
  - **OpenAI-compatible API**: an API key for OpenAI or a compatible service

Optional:

- `uv` for environment management and the `mini-coding-agent` CLI entry point
- `openai` Python package when using the OpenAI backend (`pip install openai`)

This project has no mandatory Python runtime dependency beyond the standard library for the Ollama backend, so you can run it directly with `python -m mini_coding_agent` if you do not want to use `uv`. (PyYAML is optional — install it only if you want to override the packaged YAML config; otherwise the built-in defaults are used.)

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
git clone https://github.com/xhyccc/mini-coding-agent-CLI.git
cd mini-coding-agent-CLI
```

If you forked it first, use your fork URL instead:

```bash
git clone https://github.com/<your-github-user>/mini-coding-agent-CLI.git
cd mini-coding-agent-CLI
```



&nbsp;
## Basic Usage

### Ollama backend (default)

Start the agent:

```bash
cd mini-coding-agent-CLI
uv run mini-coding-agent
```

Without `uv`, run the script directly:

```bash
cd mini-coding-agent-CLI
python -m mini_coding_agent
```

By default it uses:

- backend: `ollama`
- model: `qwen3.5:4b`
- approval: `ask`

### OpenAI-compatible backend

```bash
uv run mini-coding-agent --backend openai --model gpt-4o-mini --openai-api-key YOUR_KEY
```

Or set the key via environment variable:

```bash
export OPENAI_API_KEY=YOUR_KEY
uv run mini-coding-agent --backend openai --model gpt-4o-mini
```

To use a compatible third-party API (e.g., a local OpenAI-compatible server):

```bash
uv run mini-coding-agent --backend openai --model your-model \
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
uv run mini-coding-agent --provider kimi "List the files in this repo."

# Zhipu GLM
export ZHIPU_API_KEY=sk-...
uv run mini-coding-agent --provider glm --model glm-4-plus "Summarize README.md"

# SiliconFlow
export SILICONFLOW_API_KEY=sk-...
uv run mini-coding-agent --provider siliconflow

# Any other OpenAI-compatible endpoint
export CUSTOM_LLM_API_KEY=sk-...
uv run mini-coding-agent --provider custom \
  --openai-base-url https://my-internal-llm.example/v1 \
  --model my-internal-model
```

`--model`, `--openai-base-url`, and `--openai-api-key` always take precedence
over the preset, so you can target a specific model (e.g. `moonshot-v1-32k`)
or override the endpoint as needed.

### One-shot (non-interactive) mode

Pass a task as a positional argument to run the agent non-interactively and exit:

```bash
uv run mini-coding-agent "List the files in this repo and summarize the project."
```

Approval defaults to `auto` in one-shot mode. To override:

```bash
uv run mini-coding-agent --approval ask "Refactor the main function."
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
uv run mini-coding-agent --approval auto
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
uv run mini-coding-agent

# disable
uv run mini-coding-agent --sandbox off
```



&nbsp;
## Resume Sessions

The agent saves sessions under the target workspace root in:

```text
.mini-coding-agent/sessions/
```

Resume the latest session:

```bash
uv run mini-coding-agent --resume latest
```


Resume a specific session:

```bash
uv run mini-coding-agent --resume 20260401-144025-2dd0aa
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
uv run mini-coding-agent --help
```

Without `uv`:

```bash
python -m mini_coding_agent --help
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

&nbsp;
## Configuration via YAML

All prompts (agent identity, rules, examples, retry notices, coordinator/override layers) and harness parameters (`max_steps`, `max_new_tokens`, timeouts, sampling, `allowed_ops`, `sandbox`, `approval`, sandbox denylists) are loaded from `mini_coding_agent/config/default.yaml`. You can override any subset of them without touching the packaged file:

1. Pass `--config path/to/your.yaml` on the CLI, or
2. Drop a `.mini-coding-agent/config.yaml` file at the root of your workspace - it is auto-discovered.

Resolution order is: packaged defaults < workspace `.mini-coding-agent/config.yaml` < explicit `--config` file. Per-call CLI flags still take precedence over everything in YAML.

Project-specific rules placed in `AGENTS.md` (or `.mini-coding-agent/rules.md`) at the repo root are automatically pulled into the prompt's `<project-rules>` layer.

PyYAML is an optional dependency. Install with `pip install pyyaml` (or `pip install -e .[yaml]`) only if you want to load custom YAML; otherwise the agent uses its built-in defaults.

### Prompt architecture

The prompt is assembled in up to six XML-tagged layers ordered from most stable (top, cacheable) to most volatile (bottom):

1. `<agent-identity>` — who the agent is. **Always present.**
2. `<system-defaults>` — immutable rules, tool catalog, response examples. **Always present.**
3. `<project-rules>` — per-repo overrides (`AGENTS.md`, `.mini-coding-agent/rules.md`). Emitted only when content is found.
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
| `patch_file` | `write` | **yes** | Replace one exact text block in a file |
| `run_shell` | `bash` | **yes** | Run a shell command in the repo root |
| `run_python` | `python` | **yes** | Execute Python code in the repo root and return its output |
| `delegate` | — | no | Ask a bounded read-only child agent to investigate |

Risky tools require approval. The approval mode (`--approval`) controls whether the user is prompted (`ask`), the action is allowed automatically (`auto`), or it is always denied (`never`).

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
