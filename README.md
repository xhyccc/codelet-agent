&nbsp;
# Mini-Coding-Agent

This folder contains a small standalone coding agent:

- code: `mini_coding_agent.py`
- CLI: `mini-coding-agent`

It is a minimal local agent loop with:

- workspace snapshot collection
- stable prompt plus turn state
- structured tools
- approval handling for risky tools
- transcript and memory persistence
- bounded delegation

The agent supports two model backends: **Ollama** (default, local) and any **OpenAI-compatible API**.

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

This project has no mandatory Python runtime dependency beyond the standard library for the Ollama backend, so you can run it directly with `python mini_coding_agent.py` if you do not want to use `uv`.

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
python mini_coding_agent.py
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
python mini_coding_agent.py --help
```

CLI flags are passed before the agent starts. Use them to choose the workspace,
model backend, resume behavior, approval mode, allowed tool categories, and generation limits.

Important flags:

- `--cwd`
  sets the workspace directory the agent should inspect and modify; default: `.`
- `--backend`
  selects the model backend: `ollama` or `openai`; default: `ollama`
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
- `--max-steps`
  limits how many model and tool turns are allowed for one user request; default: `6`
- `--max-new-tokens`
  caps the model output length for each step; default: `512`
- `--temperature`
  controls sampling randomness; default: `0.2`
- `--top-p`
  controls nucleus sampling for generation; default: `0.9`

&nbsp;
## Available Tools

The agent exposes the following tools to the model. Tools are grouped into categories that can be restricted with `--allow`.

| Tool | Category | Risky | Description |
|---|---|---|---|
| `list_files` | `read` | no | List files in the workspace |
| `read_file` | `read` | no | Read a UTF-8 file by line range |
| `search` | `read` | no | Search the workspace with `rg` or a simple fallback |
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
