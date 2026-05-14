import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
HELP_TEXT = "/help, /memory, /session, /reset, /exit"
WELCOME_ART = (
    "/\\     /\\\\",
    "{  `---'  }",
    "{  O   O  }",
    "~~>  V  <~~",
    "\\\\  \\|/  /",
    "`-----'__",
)
HELP_DETAILS = "\n".join(
    [
        "Commands:",
        "/help    Show this help message.",
        "/memory  Show the agent's distilled working memory.",
        "/session Show the path to the saved session file.",
        "/reset   Clear the current session history and memory.",
        "/exit    Exit the agent.",
    ]
)
MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000
IGNORED_PATH_NAMES = {".git", ".mini-coding-agent", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}
ALL_TOOL_OPS = frozenset({"read", "write", "bash", "python"})

# Presets for popular OpenAI-compatible LLM API providers. Each preset declares the
# default base URL and the environment variable conventionally used for the API key.
# All providers below expose an OpenAI-compatible /v1/chat/completions endpoint, so
# we reuse OpenAIModelClient for every entry.
LLM_PROVIDER_PRESETS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "description": "OpenAI",
    },
    "kimi": {
        # Moonshot AI / Kimi (https://platform.moonshot.cn)
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
        "default_model": "moonshot-v1-8k",
        "description": "Moonshot AI (Kimi)",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
        "default_model": "moonshot-v1-8k",
        "description": "Moonshot AI (Kimi)",
    },
    "glm": {
        # Zhipu AI / GLM (https://open.bigmodel.cn)
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "default_model": "glm-4-flash",
        "description": "Zhipu AI (GLM)",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "default_model": "glm-4-flash",
        "description": "Zhipu AI (GLM)",
    },
    "siliconflow": {
        # SiliconFlow (https://siliconflow.cn)
        "base_url": "https://api.siliconflow.cn/v1",
        "env_key": "SILICONFLOW_API_KEY",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "description": "SiliconFlow",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "description": "DeepSeek",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "openai/gpt-4o-mini",
        "description": "OpenRouter",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "env_key": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "description": "Together AI",
    },
    "dashscope": {
        # Alibaba Cloud DashScope (Qwen) compatibility-mode endpoint
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
        "default_model": "qwen-plus",
        "description": "Alibaba DashScope (Qwen)",
    },
    "custom": {
        # Generic OpenAI-compatible endpoint; user must supply --openai-base-url
        # and either --openai-api-key or set CUSTOM_LLM_API_KEY / OPENAI_API_KEY.
        "base_url": None,
        "env_key": "CUSTOM_LLM_API_KEY",
        "default_model": None,
        "description": "Custom OpenAI-compatible endpoint",
    },
}


def resolve_provider_preset(name):
    """Look up an LLM provider preset by name (case-insensitive)."""
    if not name:
        return None
    return LLM_PROVIDER_PRESETS.get(name.lower())


###############################################
#### Lightweight Sandboxing For Risky Tools ###
###############################################
# Pattern denylist for shell commands. Each entry is a compiled regex applied
# (case-insensitive) to the whole command string. These are intentionally
# conservative: the goal is to catch obvious destructive or privilege-escalating
# patterns, not to provide a real security boundary. Always combine with
# `--approval ask` and run untrusted prompts in a real sandbox/VM/container.
SANDBOX_SHELL_DENY_PATTERNS = (
    r"\bsudo\b",
    r"\bsu\s+-",
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f?|--recursive)[^\n]*\s(/|~|\$HOME)\b",
    r"\brm\s+-rf?\s+/(?:\s|$)",
    r":\s*\(\s*\)\s*\{.*\|\s*:&\s*\}\s*;",  # classic fork bomb :(){ :|:& };:
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+0\b",
    r"\binit\s+6\b",
    r"\bchmod\s+(-R\s+)?[0-7]*777[^\n]*\s(/|~|\$HOME)(\s|$)",
    r"\bchown\s+(-R\s+)?[^\n]*\s(/|~|\$HOME)(\s|$)",
    # piping remote content directly into a shell interpreter (curl | sh, wget | bash, ...)
    r"\b(curl|wget|fetch)\b[^\n]*\|\s*(sh|bash|zsh|ksh|dash|python[0-9.]*|perl|node|ruby)\b",
    r">\s*/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|hd[a-z]+)\b",
)

# Pattern denylist for Python code passed to `run_python`. Catches obvious
# attempts to spawn root shells, write to disk devices, or remove the workspace.
SANDBOX_PYTHON_DENY_PATTERNS = (
    r"\bos\.system\s*\(\s*['\"][^'\"\n]*\b(sudo|rm\s+-rf?\s+/|mkfs|shutdown|reboot|halt|poweroff)\b",
    r"\bsubprocess\.[A-Za-z_]+\s*\([^)]*\b(sudo|mkfs|shutdown|reboot|halt|poweroff)\b",
    r"\bshutil\.rmtree\s*\(\s*['\"]/['\"]?\s*\)",
    r"\bshutil\.rmtree\s*\(\s*['\"]/(bin|boot|dev|etc|home|lib|opt|root|sbin|sys|usr|var)\b",
    r"\bopen\s*\(\s*['\"]/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|hd[a-z]+)",
)

# Environment variable name patterns to strip from sandboxed subprocess environments
# to reduce the blast radius of an accidental credential exfiltration.
SANDBOX_ENV_STRIP_PATTERNS = (
    re.compile(r"_API_KEY$"),
    re.compile(r"_TOKEN$"),
    re.compile(r"_SECRET$"),
    re.compile(r"_PASSWORD$"),
    re.compile(r"^AWS_"),
    re.compile(r"^AZURE_"),
    re.compile(r"^GCP_"),
    re.compile(r"^GITHUB_TOKEN$"),
    re.compile(r"^OPENAI_API_KEY$"),
    re.compile(r"^ANTHROPIC_API_KEY$"),
)

# Default resource limits for sandboxed subprocesses (POSIX only). We intentionally
# do NOT set RLIMIT_NPROC by default because it is enforced per real user ID,
# not per process tree, so a tight value would interact poorly with other
# processes already running under the same user (CI, IDEs, etc.).
SANDBOX_DEFAULT_LIMITS = {
    "cpu_seconds": 30,        # RLIMIT_CPU
    "address_space_bytes": 1024 * 1024 * 1024,  # RLIMIT_AS: 1 GiB
    "file_size_bytes": 64 * 1024 * 1024,        # RLIMIT_FSIZE: 64 MiB
}


def sandbox_check_shell(command):
    """Return an error message if `command` matches any denylisted pattern, else None."""
    for pattern in SANDBOX_SHELL_DENY_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"sandbox: shell command blocked by safety policy (matched: {pattern})"
    return None


def sandbox_check_python(code):
    """Return an error message if `code` matches any denylisted pattern, else None."""
    for pattern in SANDBOX_PYTHON_DENY_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return f"sandbox: python code blocked by safety policy (matched: {pattern})"
    return None


def sandbox_filter_env(env):
    """Return a copy of `env` with obviously sensitive variables removed."""
    filtered = {}
    for key, value in env.items():
        if any(pattern.search(key) for pattern in SANDBOX_ENV_STRIP_PATTERNS):
            continue
        filtered[key] = value
    # Make sure subprocesses don't accidentally inherit a writable PYTHONPATH that
    # could be used to shadow stdlib modules with attacker-controlled code.
    filtered.pop("PYTHONPATH", None)
    return filtered


def sandbox_preexec(limits=None):
    """Return a preexec_fn that applies POSIX resource limits, or None on non-POSIX."""
    if os.name != "posix":
        return None
    try:
        import resource  # noqa: F401  (imported lazily; only available on POSIX)
    except ImportError:
        return None

    limits = limits or SANDBOX_DEFAULT_LIMITS

    def _apply():
        import resource as _resource
        try:
            cpu = limits.get("cpu_seconds")
            if cpu:
                _resource.setrlimit(_resource.RLIMIT_CPU, (cpu, cpu))
            mem = limits.get("address_space_bytes")
            if mem and hasattr(_resource, "RLIMIT_AS"):
                _resource.setrlimit(_resource.RLIMIT_AS, (mem, mem))
            fsize = limits.get("file_size_bytes")
            if fsize:
                _resource.setrlimit(_resource.RLIMIT_FSIZE, (fsize, fsize))
            nproc = limits.get("max_processes")
            if nproc and hasattr(_resource, "RLIMIT_NPROC"):
                _resource.setrlimit(_resource.RLIMIT_NPROC, (nproc, nproc))
        except (ValueError, OSError):
            # If a limit can't be applied (e.g. running under existing tighter
            # limits), continue rather than crashing the subprocess launch.
            pass

    return _apply

##############################
#### Six Agent Components ####
##############################
# 1) Live Repo Context -> WorkspaceContext
# 2) Prompt Shape And Cache Reuse -> build_prefix, memory_text, prompt
# 3) Structured Tools, Validation, And Permissions -> build_tools, run_tool, validate_tool, approve, parse, path, tool_*
# 4) Context Reduction And Output Management -> clip, history_text
# 5) Transcripts, Memory, And Resumption -> SessionStore, record, note_tool, ask, reset
# 6) Delegation And Bounded Subagents -> tool_delegate


def now():
    return datetime.now(timezone.utc).isoformat()


# Supporting helper for component 4 (context reduction and output management).
def clip(text, limit=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


##############################
#### 1) Live Repo Context ####
##############################
class WorkspaceContext:
    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs

    @classmethod
    def build(cls, cwd):
        cwd = Path(cwd).resolve()

        def git(args, fallback=""):
            try:
                result = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                return result.stdout.strip() or fallback
            except Exception:
                return fallback

        repo_root = Path(git(["rev-parse", "--show-toplevel"], str(cwd))).resolve()
        docs = {}
        for base in (repo_root, cwd):
            for name in DOC_NAMES:
                path = base / name
                if not path.exists():
                    continue
                key = str(path.relative_to(repo_root))
                if key in docs:
                    continue
                docs[key] = clip(path.read_text(encoding="utf-8", errors="replace"), 1200)

        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main").removeprefix("origin/"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
        )

    def text(self):
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        return "\n".join([
            "Workspace:",
            f"- cwd: {self.cwd}",
            f"- repo_root: {self.repo_root}",
            f"- branch: {self.branch}",
            f"- default_branch: {self.default_branch}",
            "- status:",
            self.status,
            "- recent_commits:",
            commits,
            "- project_docs:",
            docs,
        ])


##############################
#### 5) Session Memory #######
##############################
class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        path.write_text(json.dumps(session, indent=2), encoding="utf-8")
        return path

    def load(self, session_id):
        return json.loads(self.path(session_id).read_text(encoding="utf-8"))

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, prompt, max_new_tokens):
        self.prompts.append(prompt)
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout

    def complete(self, prompt, max_new_tokens):
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")


class OpenAIModelClient:
    """Model client for OpenAI-compatible APIs (OpenAI, Azure OpenAI, local servers, etc.)."""

    def __init__(self, model, api_key, base_url, temperature, top_p, timeout):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else None
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout

    def complete(self, prompt, max_new_tokens):
        try:
            import openai as _openai
        except ImportError:
            raise RuntimeError(
                "The 'openai' package is required for the OpenAI backend.\n"
                "Install it with: pip install openai"
            ) from None
        kwargs = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        client = _openai.OpenAI(**kwargs)
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_new_tokens,
                temperature=self.temperature,
                top_p=self.top_p,
                timeout=self.timeout,
            )
        except _openai.APIError as exc:
            raise RuntimeError(f"OpenAI API error: {exc}") from exc
        return response.choices[0].message.content or ""


class MiniAgent:
    def __init__(
        self,
        model_client,
        workspace,
        session_store,
        session=None,
        approval_policy="ask",
        max_steps=6,
        max_new_tokens=512,
        depth=0,
        max_depth=1,
        read_only=False,
        allowed_ops=None,
        sandbox="lite",
    ):
        self.model_client = model_client
        self.workspace = workspace
        self.root = Path(workspace.repo_root)
        self.session_store = session_store
        self.approval_policy = approval_policy
        self.max_steps = max_steps
        self.max_new_tokens = max_new_tokens
        self.depth = depth
        self.max_depth = max_depth
        self.read_only = read_only
        self.allowed_ops = allowed_ops
        self.sandbox = sandbox if sandbox in ("off", "lite") else "lite"
        self.session = session or {
            "id": datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
            "created_at": now(),
            "workspace_root": workspace.repo_root,
            "history": [],
            "memory": {"task": "", "files": [], "notes": []},
        }
        self.tools = self.build_tools()
        self.prefix = self.build_prefix()
        self.session_path = self.session_store.save(self.session)

    @classmethod
    def from_session(cls, model_client, workspace, session_store, session_id, **kwargs):
        return cls(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            session=session_store.load(session_id),
            **kwargs,
        )

    @staticmethod
    def remember(bucket, item, limit):
        if not item:
            return
        if item in bucket:
            bucket.remove(item)
        bucket.append(item)
        del bucket[:-limit]

    ###############################################
    #### 3) Structured Tools And Permissions ######
    ###############################################
    def build_tools(self):
        allowed = self.allowed_ops if self.allowed_ops is not None else ALL_TOOL_OPS
        tools = {}
        if "read" in allowed:
            tools["list_files"] = {
                "schema": {"path": "str='.'"},
                "risky": False,
                "description": "List files in the workspace.",
                "run": self.tool_list_files,
            }
            tools["read_file"] = {
                "schema": {"path": "str", "start": "int=1", "end": "int=200"},
                "risky": False,
                "description": "Read a UTF-8 file by line range.",
                "run": self.tool_read_file,
            }
            tools["search"] = {
                "schema": {"pattern": "str", "path": "str='.'"},
                "risky": False,
                "description": "Search the workspace with rg or a simple fallback.",
                "run": self.tool_search,
            }
        if "bash" in allowed:
            tools["run_shell"] = {
                "schema": {"command": "str", "timeout": "int=20"},
                "risky": True,
                "description": "Run a shell command in the repo root.",
                "run": self.tool_run_shell,
            }
        if "write" in allowed:
            tools["write_file"] = {
                "schema": {"path": "str", "content": "str"},
                "risky": True,
                "description": "Write a text file.",
                "run": self.tool_write_file,
            }
            tools["patch_file"] = {
                "schema": {"path": "str", "old_text": "str", "new_text": "str"},
                "risky": True,
                "description": "Replace one exact text block in a file.",
                "run": self.tool_patch_file,
            }
        if "python" in allowed:
            tools["run_python"] = {
                "schema": {"code": "str", "timeout": "int=20"},
                "risky": True,
                "description": "Execute Python code in the repo root and return its output.",
                "run": self.tool_run_python,
            }
        if self.depth < self.max_depth:
            tools["delegate"] = {
                "schema": {"task": "str", "max_steps": "int=3"},
                "risky": False,
                "description": "Ask a bounded read-only child agent to investigate.",
                "run": self.tool_delegate,
            }
        return tools

    ############################################
    #### 2) Prompt Shape And Cache Reuse #######
    ############################################
    def build_prefix(self):
        tool_lines = []
        for name, tool in self.tools.items():
            fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
            risk = "approval required" if tool["risky"] else "safe"
            tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
        tool_text = "\n".join(tool_lines)
        all_examples = {
            "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
            "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
            "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
            "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
            "run_python": '<tool>{"name":"run_python","args":{"code":"import sys; print(sys.version)","timeout":20}}</tool>',
        }
        example_lines = [v for name, v in all_examples.items() if name in self.tools]
        example_lines.append("<final>Done.</final>")
        examples = "\n".join(example_lines)
        rules = "\n".join([
            "- Use tools instead of guessing about the workspace.",
            "- Return exactly one <tool>...</tool> or one <final>...</final>.",
            "- Tool calls must look like:",
            '  <tool>{"name":"tool_name","args":{...}}</tool>',
            "- For write_file and patch_file with multi-line text, prefer XML style:",
            '  <tool name="write_file" path="file.py"><content>...</content></tool>',
            "- Final answers must look like:",
            "  <final>your answer</final>",
            "- Never invent tool results.",
            "- Keep answers concise and concrete.",
            "- If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.",
            "- Before writing tests for existing code, read the implementation first.",
            "- When writing tests, match the current implementation unless the user explicitly asked you to change the code.",
            "- New files should be complete and runnable, including obvious imports.",
            "- Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.",
            "- Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, run_python, or delegate with args={}.",
        ])
        return "\n\n".join([
            "You are Mini-Coding-Agent, a small coding agent.",
            "Rules:\n" + rules,
            "Tools:\n" + tool_text,
            "Valid response examples:\n" + examples,
            self.workspace.text(),
        ])

    def memory_text(self):
        memory = self.session["memory"]
        notes = "\n".join(f"- {note}" for note in memory["notes"]) or "- none"
        return "\n".join([
            "Memory:",
            f"- task: {memory['task'] or '-'}",
            f"- files: {', '.join(memory['files']) or '-'}",
            "- notes:",
            notes,
        ])

    #####################################################
    #### 4) Context Reduction And Output Management #####
    #####################################################
    def history_text(self):
        history = self.session["history"]
        if not history:
            return "- empty"

        lines = []
        seen_reads = set()
        recent_start = max(0, len(history) - 6)
        for index, item in enumerate(history):
            recent = index >= recent_start
            if item["role"] == "tool" and item["name"] in ("write_file", "patch_file"):
                path = str(item["args"].get("path", ""))
                seen_reads.discard(path)
            if item["role"] == "tool" and item["name"] == "read_file" and not recent:
                path = str(item["args"].get("path", ""))
                if path in seen_reads:
                    continue
                seen_reads.add(path)

            if item["role"] == "tool":
                limit = 900 if recent else 180
                lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
                lines.append(clip(item["content"], limit))
            else:
                limit = 900 if recent else 220
                lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

        return clip("\n".join(lines), MAX_HISTORY)

    ########################################################
    #### 2) Prompt Shape And Cache Reuse (Continued) #######
    ########################################################
    def prompt(self, user_message):
        return "\n\n".join([
            self.prefix,
            self.memory_text(),
            "Transcript:\n" + self.history_text(),
            "Current user request:\n" + user_message,
        ])

    ###############################################
    #### 5) Session Memory (Continued) ###########
    ###############################################
    def record(self, item):
        self.session["history"].append(item)
        self.session_path = self.session_store.save(self.session)

    def note_tool(self, name, args, result):
        memory = self.session["memory"]
        path = args.get("path")
        if name in {"read_file", "write_file", "patch_file"} and path:
            self.remember(memory["files"], str(path), 8)
        note = f"{name}: {clip(str(result).replace(chr(10), ' '), 220)}"
        self.remember(memory["notes"], note, 5)

    def ask(self, user_message):
        memory = self.session["memory"]
        if not memory["task"]:
            memory["task"] = clip(user_message.strip(), 300)
        self.record({"role": "user", "content": user_message, "created_at": now()})

        tool_steps = 0
        attempts = 0
        max_attempts = max(self.max_steps * 3, self.max_steps + 4)

        while tool_steps < self.max_steps and attempts < max_attempts:
            attempts += 1
            raw = self.model_client.complete(self.prompt(user_message), self.max_new_tokens)
            kind, payload = self.parse(raw)

            if kind == "tool":
                tool_steps += 1
                name = payload.get("name", "")
                args = payload.get("args", {})
                result = self.run_tool(name, args)
                self.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
                )
                self.note_tool(name, args, result)
                continue

            if kind == "retry":
                self.record({"role": "assistant", "content": payload, "created_at": now()})
                continue

            final = (payload or raw).strip()
            self.record({"role": "assistant", "content": final, "created_at": now()})
            self.remember(memory["notes"], clip(final, 220), 5)
            return final

        if attempts >= max_attempts and tool_steps < self.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
        else:
            final = "Stopped after reaching the step limit without a final answer."
        self.record({"role": "assistant", "content": final, "created_at": now()})
        return final

    #############################################################
    #### 3) Structured Tools, Validation, And Permissions #######
    #############################################################
    def run_tool(self, name, args):
        tool = self.tools.get(name)
        if tool is None:
            return f"error: unknown tool '{name}'"
        try:
            self.validate_tool(name, args)
        except Exception as exc:
            example = self.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            return message
        if self.repeated_tool_call(name, args):
            return f"error: repeated identical tool call for {name}; choose a different tool or return a final answer"
        if tool["risky"] and not self.approve(name, args):
            return f"error: approval denied for {name}"
        try:
            return clip(tool["run"](args))
        except Exception as exc:
            return f"error: tool {name} failed: {exc}"

    def repeated_tool_call(self, name, args):
        tool_events = [item for item in self.session["history"] if item["role"] == "tool"]
        if len(tool_events) < 2:
            return False
        recent = tool_events[-2:]
        return all(item["name"] == name and item["args"] == args for item in recent)

    def tool_example(self, name):
        examples = {
            "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
            "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
            "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
            "run_python": '<tool>{"name":"run_python","args":{"code":"import sys; print(sys.version)","timeout":20}}</tool>',
            "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
            "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
            "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
        }
        return examples.get(name, "")

    def validate_tool(self, name, args):
        args = args or {}

        if name == "list_files":
            path = self.path(args.get("path", "."))
            if not path.is_dir():
                raise ValueError("path is not a directory")
            return

        if name == "read_file":
            path = self.path(args["path"])
            if not path.is_file():
                raise ValueError("path is not a file")
            start = int(args.get("start", 1))
            end = int(args.get("end", 200))
            if start < 1 or end < start:
                raise ValueError("invalid line range")
            return

        if name == "search":
            pattern = str(args.get("pattern", "")).strip()
            if not pattern:
                raise ValueError("pattern must not be empty")
            self.path(args.get("path", "."))
            return

        if name == "run_shell":
            command = str(args.get("command", "")).strip()
            if not command:
                raise ValueError("command must not be empty")
            timeout = int(args.get("timeout", 20))
            if timeout < 1 or timeout > 120:
                raise ValueError("timeout must be in [1, 120]")
            return

        if name == "write_file":
            path = self.path(args["path"])
            if path.exists() and path.is_dir():
                raise ValueError("path is a directory")
            if "content" not in args:
                raise ValueError("missing content")
            return

        if name == "patch_file":
            path = self.path(args["path"])
            if not path.is_file():
                raise ValueError("path is not a file")
            old_text = str(args.get("old_text", ""))
            if not old_text:
                raise ValueError("old_text must not be empty")
            if "new_text" not in args:
                raise ValueError("missing new_text")
            text = path.read_text(encoding="utf-8")
            count = text.count(old_text)
            if count != 1:
                raise ValueError(f"old_text must occur exactly once, found {count}")
            return

        if name == "delegate":
            if self.depth >= self.max_depth:
                raise ValueError("delegate depth exceeded")
            task = str(args.get("task", "")).strip()
            if not task:
                raise ValueError("task must not be empty")
            return

        if name == "run_python":
            code = str(args.get("code", "")).strip()
            if not code:
                raise ValueError("code must not be empty")
            timeout = int(args.get("timeout", 20))
            if timeout < 1 or timeout > 120:
                raise ValueError("timeout must be in [1, 120]")
            return

    def approve(self, name, args):
        if self.read_only:
            return False
        if self.approval_policy == "auto":
            return True
        if self.approval_policy == "never":
            return False
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def parse(raw):
        raw = str(raw)
        if "<tool>" in raw and ("<final>" not in raw or raw.find("<tool>") < raw.find("<final>")):
            body = MiniAgent.extract(raw, "tool")
            try:
                payload = json.loads(body)
            except Exception:
                return "retry", MiniAgent.retry_notice("model returned malformed tool JSON")
            if not isinstance(payload, dict):
                return "retry", MiniAgent.retry_notice("tool payload must be a JSON object")
            if not str(payload.get("name", "")).strip():
                return "retry", MiniAgent.retry_notice("tool payload is missing a tool name")
            args = payload.get("args", {})
            if args is None:
                payload["args"] = {}
            elif not isinstance(args, dict):
                return "retry", MiniAgent.retry_notice()
            return "tool", payload
        if "<tool" in raw and ("<final>" not in raw or raw.find("<tool") < raw.find("<final>")):
            payload = MiniAgent.parse_xml_tool(raw)
            if payload is not None:
                return "tool", payload
            return "retry", MiniAgent.retry_notice()
        if "<final>" in raw:
            final = MiniAgent.extract(raw, "final").strip()
            if final:
                return "final", final
            return "retry", MiniAgent.retry_notice("model returned an empty <final> answer")
        raw = raw.strip()
        if raw:
            return "final", raw
        return "retry", MiniAgent.retry_notice("model returned an empty response")

    @staticmethod
    def retry_notice(problem=None):
        prefix = "Runtime notice"
        if problem:
            prefix += f": {problem}"
        else:
            prefix += ": model returned malformed tool output"
        return (
            f"{prefix}. Reply with a valid <tool> call or a non-empty <final> answer. "
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        )

    @staticmethod
    def parse_xml_tool(raw):
        match = re.search(r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", raw, re.S)
        if not match:
            return None
        attrs = MiniAgent.parse_attrs(match.group("attrs"))
        name = str(attrs.pop("name", "")).strip()
        if not name:
            return None

        body = match.group("body")
        args = dict(attrs)
        for key in ("content", "old_text", "new_text", "command", "task", "pattern", "path"):
            if f"<{key}>" in body:
                args[key] = MiniAgent.extract_raw(body, key)

        body_text = body.strip("\n")
        if name == "write_file" and "content" not in args and body_text:
            args["content"] = body_text
        if name == "delegate" and "task" not in args and body_text:
            args["task"] = body_text.strip()
        return {"name": name, "args": args}

    @staticmethod
    def parse_attrs(text):
        attrs = {}
        for match in re.finditer(r"""([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""", text):
            attrs[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
        return attrs

    @staticmethod
    def extract(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:].strip()
        return text[start:end].strip()

    @staticmethod
    def extract_raw(text, tag):
        start_tag = f"<{tag}>"
        end_tag = f"</{tag}>"
        start = text.find(start_tag)
        if start == -1:
            return text
        start += len(start_tag)
        end = text.find(end_tag, start)
        if end == -1:
            return text[start:]
        return text[start:end]

    def reset(self):
        self.session["history"] = []
        self.session["memory"] = {"task": "", "files": [], "notes": []}
        self.session_store.save(self.session)

    def path_is_within_root(self, resolved):
        probe = resolved
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        for candidate in (probe, *probe.parents):
            try:
                if candidate.samefile(self.root):
                    return True
            except OSError:
                continue
        return False

    def path(self, raw_path):
        path = Path(raw_path)
        path = path if path.is_absolute() else self.root / path
        resolved = path.resolve()
        if not self.path_is_within_root(resolved):
            raise ValueError(f"path escapes workspace: {raw_path}")
        return resolved

    def tool_list_files(self, args):
        path = self.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        entries = [
            item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
            if item.name not in IGNORED_PATH_NAMES
        ]
        lines = []
        for entry in entries[:200]:
            kind = "[D]" if entry.is_dir() else "[F]"
            lines.append(f"{kind} {entry.relative_to(self.root)}")
        return "\n".join(lines) or "(empty)"

    def tool_read_file(self, args):
        path = self.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
        return f"# {path.relative_to(self.root)}\n{body}"

    def tool_search(self, args):
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        path = self.path(args.get("path", "."))

        if shutil.which("rg"):
            result = subprocess.run(
                ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() or result.stderr.strip() or "(no matches)"

        matches = []
        files = [path] if path.is_file() else [
            item for item in path.rglob("*")
            if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(self.root).parts)
        ]
        for file_path in files:
            for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if pattern.lower() in line.lower():
                    matches.append(f"{file_path.relative_to(self.root)}:{number}:{line}")
                    if len(matches) >= 200:
                        return "\n".join(matches)
        return "\n".join(matches) or "(no matches)"

    def tool_run_shell(self, args):
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")

        sandbox_kwargs = {}
        if self.sandbox == "lite":
            blocked = sandbox_check_shell(command)
            if blocked:
                return blocked
            sandbox_kwargs["env"] = sandbox_filter_env(os.environ)
            preexec = sandbox_preexec()
            if preexec is not None:
                sandbox_kwargs["preexec_fn"] = preexec

        result = subprocess.run(
            command,
            cwd=self.root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            **sandbox_kwargs,
        )
        return "\n".join(
            [
                f"exit_code: {result.returncode}",
                "stdout:",
                result.stdout.strip() or "(empty)",
                "stderr:",
                result.stderr.strip() or "(empty)",
            ]
        )

    def tool_write_file(self, args):
        path = self.path(args["path"])
        content = str(args["content"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {path.relative_to(self.root)} ({len(content)} chars)"

    def tool_patch_file(self, args):
        path = self.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
        return f"patched {path.relative_to(self.root)}"

    def tool_run_python(self, args):
        code = str(args.get("code", "")).strip()
        if not code:
            raise ValueError("code must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")

        sandbox_kwargs = {}
        if self.sandbox == "lite":
            blocked = sandbox_check_python(code)
            if blocked:
                return blocked
            sandbox_kwargs["env"] = sandbox_filter_env(os.environ)
            preexec = sandbox_preexec()
            if preexec is not None:
                sandbox_kwargs["preexec_fn"] = preexec

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as fh:
            fh.write(code)
            tmp_path = fh.name
        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                **sandbox_kwargs,
            )
        finally:
            os.unlink(tmp_path)
        return "\n".join(
            [
                f"exit_code: {result.returncode}",
                "stdout:",
                result.stdout.strip() or "(empty)",
                "stderr:",
                result.stderr.strip() or "(empty)",
            ]
        )

    ###################################################
    #### 6) Delegation And Bounded Subagents ##########
    ###################################################
    def tool_delegate(self, args):
        if self.depth >= self.max_depth:
            raise ValueError("delegate depth exceeded")
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        child = MiniAgent(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=self.session_store,
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=self.max_new_tokens,
            depth=self.depth + 1,
            max_depth=self.max_depth,
            read_only=True,
            allowed_ops=self.allowed_ops,
            sandbox=self.sandbox,
        )
        child.session["memory"]["task"] = task
        child.session["memory"]["notes"] = [clip(self.history_text(), 300)]
        return "delegate_result:\n" + child.ask(task)


def build_welcome(agent, model, host=None, *, backend="ollama"):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center("MINI CODING AGENT"),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BACKEND", backend),
            pair("APPROVAL", agent.approval_policy, "BRANCH", agent.workspace.branch),
            row("SESSION  " + middle(agent.session["id"], inner - 9)),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    workspace = WorkspaceContext.build(args.cwd)
    store = SessionStore(Path(workspace.repo_root) / ".mini-coding-agent" / "sessions")

    # If the user picked an OpenAI-compatible provider preset (Kimi, GLM,
    # SiliconFlow, ...) treat it like the `openai` backend with prefilled
    # base_url and api key env var. `--provider` always wins over `--backend`
    # because it is the more specific signal.
    preset = resolve_provider_preset(args.provider) if getattr(args, "provider", None) else None
    if preset is not None:
        args.backend = "openai"
        # Fill in base_url from the preset unless the user explicitly overrode it.
        if not args.openai_base_url_explicit and preset.get("base_url"):
            args.openai_base_url = preset["base_url"]
        # Fill in API key from the preset's conventional env var if not given.
        if not args.openai_api_key:
            args.openai_api_key = os.environ.get(preset["env_key"]) or os.environ.get("OPENAI_API_KEY")
        # Fill in default model if user didn't specify one explicitly.
        if not args.model_explicit and preset.get("default_model"):
            args.model = preset["default_model"]

    if args.backend == "openai":
        api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            hint = ""
            if preset is not None:
                hint = f" (e.g. export {preset['env_key']}=...)"
            raise RuntimeError(
                "OpenAI-compatible API key is required."
                f" Set --openai-api-key or the OPENAI_API_KEY environment variable{hint}."
            )
        model_client = OpenAIModelClient(
            model=args.model,
            api_key=api_key,
            base_url=args.openai_base_url,
            temperature=args.temperature,
            top_p=args.top_p,
            timeout=args.openai_timeout,
        )
    else:
        model_client = OllamaModelClient(
            model=args.model,
            host=args.host,
            temperature=args.temperature,
            top_p=args.top_p,
            timeout=args.ollama_timeout,
        )

    allowed_ops = set(args.allow) if args.allow else None

    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        return MiniAgent.from_session(
            model_client=model_client,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            allowed_ops=allowed_ops,
            sandbox=args.sandbox,
        )
    return MiniAgent(
        model_client=model_client,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        allowed_ops=allowed_ops,
        sandbox=args.sandbox,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent supporting Ollama and OpenAI-compatible backends.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot task prompt (runs non-interactively with auto-approval by default).")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--backend",
        choices=("ollama", "openai"),
        default="ollama",
        help="Model backend to use.",
    )
    provider_choices = sorted(LLM_PROVIDER_PRESETS.keys())
    parser.add_argument(
        "--provider",
        choices=provider_choices,
        default=None,
        help=(
            "Convenience preset for a custom OpenAI-compatible LLM API. "
            "Sets --backend openai and an appropriate --openai-base-url, and reads "
            "the API key from the provider's conventional environment variable "
            "(e.g. MOONSHOT_API_KEY for kimi, ZHIPU_API_KEY for glm, "
            "SILICONFLOW_API_KEY for siliconflow). Pick 'custom' to combine with "
            "--openai-base-url and --openai-api-key for any other endpoint."
        ),
    )
    parser.add_argument("--model", default=argparse.SUPPRESS, help="Model name (Ollama model or OpenAI model id). Default: qwen3.5:4b, or provider preset default.")
    # Ollama-specific flags
    parser.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama server URL.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    # OpenAI-specific flags
    parser.add_argument("--openai-api-key", default=None, help="OpenAI API key (falls back to OPENAI_API_KEY env var, or the preset-specific env var when --provider is set).")
    parser.add_argument("--openai-base-url", default=argparse.SUPPRESS, help="Base URL for OpenAI-compatible API. Default: https://api.openai.com/v1, or provider preset value.")
    parser.add_argument("--openai-timeout", type=int, default=60, help="OpenAI request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default=None,
        help="Approval policy for risky tools. Defaults to 'auto' when a task prompt is given (delegation mode), 'ask' for interactive mode.",
    )
    parser.add_argument(
        "--allow",
        nargs="+",
        choices=("read", "write", "bash", "python"),
        default=None,
        metavar="OP",
        help="Allowed tool categories: read, write, bash, python. Defaults to all when not specified.",
    )
    parser.add_argument(
        "--sandbox",
        choices=("off", "lite"),
        default="lite",
        help=(
            "Lightweight sandboxing for risky tools (run_shell, run_python). "
            "'lite' (default) blocks obviously destructive command patterns, "
            "strips sensitive environment variables from subprocesses, and applies "
            "POSIX resource limits (CPU, memory, file size, processes). 'off' "
            "disables all of the above. This is best-effort defense in depth, "
            "not a true security boundary."
        ),
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p nucleus sampling value.")
    return parser


def _post_process_args(args):
    """Apply defaults that depend on whether the user explicitly passed flags."""
    args.model_explicit = hasattr(args, "model")
    if not args.model_explicit:
        args.model = "qwen3.5:4b"
    args.openai_base_url_explicit = hasattr(args, "openai_base_url")
    if not args.openai_base_url_explicit:
        args.openai_base_url = "https://api.openai.com/v1"
    return args


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    args = _post_process_args(args)

    # Default approval: "auto" for non-interactive task runs (delegation mode), "ask" for REPL.
    if args.approval is None:
        args.approval = "auto" if args.prompt else "ask"

    agent = build_agent(args)

    backend_label = args.backend
    if getattr(args, "provider", None):
        backend_label = f"{args.backend} ({args.provider})"
    print(build_welcome(agent, model=args.model, backend=backend_label))

    if args.prompt:
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                print(agent.ask(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    while True:
        try:
            user_input = input("\nmini-coding-agent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            print(HELP_DETAILS)
            continue
        if user_input == "/memory":
            print(agent.memory_text())
            continue
        if user_input == "/session":
            print(agent.session_path)
            continue
        if user_input == "/reset":
            agent.reset()
            print("session reset")
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
