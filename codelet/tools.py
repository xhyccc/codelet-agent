"""Tool implementations and the per-agent tool registry.

The :class:`ToolRegistry` builds the dictionary of available tools based on the
agent's permissions (``allowed_ops``) and delegation budget. Each tool is a
small Python callable bound to a host :class:`~codelet.agent.MiniAgent`
so it can access the workspace and sandbox configuration.
"""

import html as _html_module
import itertools
import json
import os
import platform
import re as _re
import shutil
import subprocess
import sys
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from difflib import unified_diff
from html.parser import HTMLParser
from pathlib import Path

from .sandbox import (
    sandbox_check_python,
    sandbox_check_shell,
    sandbox_filter_env,
    sandbox_preexec,
)
from .utils import (
    ALL_TOOL_OPS,
    IGNORED_PATH_NAMES,
    clip_head_tail,
    dedupe_lines,
    strip_ansi,
)


# Read-only tools that have no side effects and therefore may be executed
# concurrently when the model emits several of them in a single turn. This
# mirrors the reference agent's ``isConcurrencySafe`` flag and tool-call
# partitioning: maximal consecutive runs of these tools are dispatched in
# parallel, while write/exec/delegate tools always run serially and in order.
CONCURRENCY_SAFE_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search",
        "glob",
        "web_search",
        "web_fetch",
        "load_skill",
    }
)


def is_concurrency_safe(name):
    """Return True if a tool named ``name`` is safe to run in parallel."""
    return name in CONCURRENCY_SAFE_TOOLS


def _scrub_subprocess_text(text, *, limit):
    """Strip ANSI, dedupe repeated lines, and head/tail-clip ``text``."""
    if not text:
        return text
    return clip_head_tail(dedupe_lines(strip_ansi(text)), limit)


def _render_diff(before, after, label, *, max_lines=40):
    """Render a small unified diff between two strings for tool feedback."""
    lines = list(
        unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=label,
            tofile=label,
            n=2,
            lineterm="",
        )
    )
    if not lines:
        return ""
    if len(lines) > max_lines:
        head = lines[: max_lines - 4]
        tail = lines[-4:]
        return "\n".join(head + [f"... [{len(lines) - max_lines} more diff lines clipped] ..."] + tail)
    return "\n".join(lines)


def _is_windows():
    return platform.system().lower().startswith("win")


def _windows_shell_command(command):
    """Wrap a command for Windows: prefer PowerShell, else cmd.exe."""
    if shutil.which("pwsh"):
        return ["pwsh", "-NoLogo", "-NoProfile", "-Command", command]
    if shutil.which("powershell"):
        return ["powershell", "-NoLogo", "-NoProfile", "-Command", command]
    return ["cmd.exe", "/C", command]


class ToolRegistry:
    """Builds the dict of tools available to a given agent.

    The registry intentionally has no state of its own besides the host agent;
    it exists to keep the tool-construction logic out of MiniAgent's body.
    """

    def __init__(self, agent):
        self.agent = agent

    def build(self):
        agent = self.agent
        allowed = agent.allowed_ops if agent.allowed_ops is not None else ALL_TOOL_OPS
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
            tools["glob"] = {
                "schema": {"pattern": "str", "path": "str='.'"},
                "risky": False,
                "description": "List workspace files matching a glob pattern (e.g. **/*.py).",
                "run": self.tool_glob,
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
            tools["delete_file"] = {
                "schema": {"path": "str"},
                "risky": True,
                "description": (
                    "Move a file or empty directory into the workspace trash "
                    "(.codelet/trash/<session-id>/). Reversible."
                ),
                "run": self.tool_delete_file,
            }
            tools["move_file"] = {
                "schema": {"src": "str", "dst": "str"},
                "risky": True,
                "description": "Rename or move a file within the workspace.",
                "run": self.tool_move_file,
            }
        if "python" in allowed:
            tools["run_python"] = {
                "schema": {"code": "str", "timeout": "int=20"},
                "risky": True,
                "description": "Execute Python code in the repo root and return its output.",
                "run": self.tool_run_python,
            }
        if agent.depth < agent.max_depth:
            _harness = agent.config.get("harness", {})
            _delegate_default_steps = int(_harness.get("delegate_max_steps", 100))
            tools["delegate"] = {
                "schema": {"task": "str", "max_steps": f"int={_delegate_default_steps}"},
                "risky": False,
                "description": (
                    "Ask a bounded read-only child agent to investigate. "
                    "Child agents cannot write files, run shell commands, or run Python — "
                    "only use for research and inspection tasks."
                ),
                "run": self.tool_delegate,
            }
            tools["delegate_parallel"] = {
                "schema": {"tasks": "list[str]", "max_steps": f"int={_delegate_default_steps}"},
                "risky": False,
                "description": (
                    "Run multiple bounded read-only child agents in parallel. "
                    "Children cannot write files, run shell commands, or run Python — "
                    "only use for research and inspection tasks. "
                    "Each task is a string; returns a JSON list of {task, result}."
                ),
                "run": self.tool_delegate_parallel,
            }
        if "net" in allowed:
            tools["web_search"] = {
                "schema": {"query": "str", "max_results": "int=5"},
                "risky": False,
                "description": (
                    "Search the web via DuckDuckGo (no API key required). "
                    "Returns titles, URLs, and snippets."
                ),
                "run": self.tool_web_search,
            }
            tools["web_fetch"] = {
                "schema": {"url": "str", "max_chars": "int=4000"},
                "risky": False,
                "description": (
                    "Fetch a URL and return its plain-text content "
                    "(HTML tags stripped)."
                ),
                "run": self.tool_web_fetch,
            }
        # Progressive-disclosure skills: only enabled when skills were
        # discovered at agent-construction time. Provides a way to load a
        # skill body on demand without paying its token cost upfront.
        if getattr(agent, "skills", None):
            tools["load_skill"] = {
                "schema": {"name": "str"},
                "risky": False,
                "description": (
                    "Load the body of a discovered skill (see <skills> in prompt). "
                    "Returns the full SKILL.md body plus its asset manifest."
                ),
                "run": self.tool_load_skill,
            }
        if "write" in allowed:
            tools["remember_fact"] = {
                "schema": {"fact": "str"},
                "risky": False,
                "description": (
                    "Append a one-line fact to .codelet/repo-memory.md "
                    "so future sessions inherit it."
                ),
                "run": self.tool_remember_fact,
            }
        tools["decompose"] = {
            "schema": {"goal": "str", "steps": "list[str]=[]"},
            "risky": False,
            "description": (
                "Record a multi-step plan for the current task. "
                "Provide an explicit list of steps, or omit steps to auto-split "
                "the goal string by sentence boundaries."
            ),
            "run": self.tool_decompose,
        }
        # MCP tools: register tools from connected MCP servers
        for client in getattr(agent, "mcp_clients", []):
            for mcp_tool in client.list_tools():
                tools[mcp_tool["name"]] = {
                    "schema": mcp_tool.get("schema", {}),
                    "risky": False,
                    "description": mcp_tool.get("description", "MCP tool"),
                    "run": lambda args, _name=mcp_tool["name"]: f"MCP tool {_name} executed with {args}",
                }
        return tools

    # ---- read tools -----------------------------------------------------

    def tool_list_files(self, args):
        agent = self.agent
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        entries = [
            item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
            if item.name not in IGNORED_PATH_NAMES
        ]
        lines = []
        for entry in entries[:200]:
            kind = "[D]" if entry.is_dir() else "[F]"
            lines.append(f"{kind} {entry.relative_to(agent.root)}")
        return "\n".join(lines) or "(empty)"

    def tool_read_file(self, args):
        agent = self.agent
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = list(itertools.islice(f, start - 1, end))
        body = "\n".join(
            f"{number:>4}: {text}"
            for number, line in enumerate(lines, start=start)
            for text in (line.rstrip("\r\n"),)
        )
        return f"# {path.relative_to(agent.root)}\n{body}"

    def tool_search(self, args):
        agent = self.agent
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        path = agent.path(args.get("path", "."))

        if shutil.which("rg"):
            result = subprocess.run(
                ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
                cwd=agent.root,
                capture_output=True,
                text=True,
            )
            return result.stdout.strip() or result.stderr.strip() or "(no matches)"

        matches = []
        files = [path] if path.is_file() else [
            item for item in path.rglob("*")
            if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(agent.root).parts)
        ]
        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    for number, line in enumerate(f, start=1):
                        if pattern.lower() in line.lower():
                            matches.append(f"{file_path.relative_to(agent.root)}:{number}:{line.strip()}")
                            if len(matches) >= 200:
                                return "\n".join(matches)
            except OSError:
                pass
        return "\n".join(matches) or "(no matches)"

    def tool_glob(self, args):
        """List files matching a glob pattern (e.g. ``**/*.py``)."""
        agent = self.agent
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        base = agent.path(args.get("path", "."))
        if not base.is_dir():
            raise ValueError("path is not a directory")
        matches = []
        for match in sorted(base.glob(pattern)):
            try:
                relative = match.relative_to(agent.root)
            except ValueError:
                continue
            if any(part in IGNORED_PATH_NAMES for part in relative.parts):
                continue
            kind = "[D]" if match.is_dir() else "[F]"
            matches.append(f"{kind} {relative}")
            if len(matches) >= 200:
                break
        return "\n".join(matches) or "(no matches)"

    # ---- shell / python tools ------------------------------------------

    def tool_run_shell(self, args):
        agent = self.agent
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        harness = agent.config.get("harness", {})
        max_timeout = int(harness.get("tool_max_timeout", 120))
        timeout = int(args.get("timeout", harness.get("tool_timeout", 20)))
        if timeout < 1 or timeout > max_timeout:
            raise ValueError(f"timeout must be in [1, {max_timeout}]")

        sandbox_kwargs = {}
        if agent.sandbox == "lite":
            blocked = sandbox_check_shell(command)
            if blocked:
                return blocked
            sandbox_kwargs["env"] = sandbox_filter_env(os.environ)
            preexec = sandbox_preexec()
            if preexec is not None:
                sandbox_kwargs["preexec_fn"] = preexec

        if _is_windows():
            # PowerShell preferred; cmd.exe fallback. preexec_fn / rlimits
            # are POSIX-only and have already been gated out by
            # sandbox_preexec() returning None on Windows.
            argv = _windows_shell_command(command)
            try:
                result = subprocess.run(
                    argv,
                    cwd=agent.root,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    **{k: v for k, v in sandbox_kwargs.items() if k != "preexec_fn"},
                )
                stdout_text = result.stdout
                stderr_text = result.stderr
                exit_code = result.returncode
            except subprocess.TimeoutExpired as exc:
                stdout_text = exc.stdout or ""
                stderr_text = exc.stderr or ""
                exit_code = f"timeout ({timeout}s)"
        else:
            try:
                result = subprocess.run(
                    command,
                    cwd=agent.root,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    **sandbox_kwargs,
                )
                stdout_text = result.stdout
                stderr_text = result.stderr
                exit_code = result.returncode
            except subprocess.TimeoutExpired as exc:
                stdout_text = exc.stdout or ""
                stderr_text = exc.stderr or ""
                exit_code = f"timeout ({timeout}s)"
        tool_out_cfg = (harness.get("tool_output") or {})
        out_limit = int(tool_out_cfg.get("max_chars", harness.get("max_tool_output", 4000)))
        return "\n".join(
            [
                f"exit_code: {exit_code}",
                "stdout:",
                _scrub_subprocess_text(stdout_text, limit=out_limit).strip() or "(empty)",
                "stderr:",
                _scrub_subprocess_text(stderr_text, limit=out_limit).strip() or "(empty)",
            ]
        )

    def tool_run_python(self, args):
        agent = self.agent
        code = str(args.get("code", "")).strip()
        if not code:
            raise ValueError("code must not be empty")
        harness = agent.config.get("harness", {})
        max_timeout = int(harness.get("tool_max_timeout", 120))
        timeout = int(args.get("timeout", harness.get("tool_timeout", 20)))
        if timeout < 1 or timeout > max_timeout:
            raise ValueError(f"timeout must be in [1, {max_timeout}]")

        sandbox_kwargs = {}
        if agent.sandbox == "lite":
            blocked = sandbox_check_python(code)
            if blocked:
                return blocked
            sandbox_kwargs["env"] = sandbox_filter_env(os.environ)
            preexec = sandbox_preexec()
            if preexec is not None:
                sandbox_kwargs["preexec_fn"] = preexec

        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=agent.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                **sandbox_kwargs,
            )
            stdout_text = result.stdout
            stderr_text = result.stderr
            exit_code = result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout_text = exc.stdout or ""
            stderr_text = exc.stderr or ""
            exit_code = f"timeout ({timeout}s)"
        tool_out_cfg = (harness.get("tool_output") or {})
        out_limit = int(tool_out_cfg.get("max_chars", harness.get("max_tool_output", 4000)))
        return "\n".join(
            [
                f"exit_code: {exit_code}",
                "stdout:",
                _scrub_subprocess_text(stdout_text, limit=out_limit).strip() or "(empty)",
                "stderr:",
                _scrub_subprocess_text(stderr_text, limit=out_limit).strip() or "(empty)",
            ]
        )

    # ---- write tools ----------------------------------------------------

    def tool_write_file(self, args):
        agent = self.agent
        path = agent.path(args["path"])
        content = str(args["content"])
        # Snapshot before write
        from .file_history import create_snapshot
        rel = path.relative_to(agent.root)
        create_snapshot(str(agent.root), str(rel))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {rel} ({len(content)} chars)"

    def tool_patch_file(self, args):
        agent = self.agent
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        # Snapshot before patch
        from .file_history import create_snapshot
        rel = path.relative_to(agent.root)
        create_snapshot(str(agent.root), str(rel))
        text = path.read_text(encoding="utf-8")
        norm_text = text.replace("\r\n", "\n")
        norm_old = old_text.replace("\r\n", "\n")
        count = norm_text.count(norm_old)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        new_text = str(args["new_text"]).replace("\r\n", "\n")
        updated = norm_text.replace(norm_old, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        diff = _render_diff(text, updated, str(rel))
        return f"patched {rel}\n{diff}" if diff else f"patched {rel}"

    def _trash_dir(self):
        agent = self.agent
        session_id = (agent.session or {}).get("id", "unknown")
        trash = agent.root / ".codelet" / "trash" / session_id
        trash.mkdir(parents=True, exist_ok=True)
        return trash

    def tool_delete_file(self, args):
        agent = self.agent
        path = agent.path(args["path"])
        if path.resolve() == agent.root.resolve():
            raise ValueError("error: Refusing to delete the workspace root.")
        if not path.exists():
            raise ValueError("path does not exist")
        if path.is_dir() and any(path.iterdir()):
            raise ValueError("directory is not empty (refusing to recurse)")
        trash = self._trash_dir()
        rel = path.relative_to(agent.root)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        target = trash / f"{stamp}-{uuid.uuid4().hex[:6]}-{path.name}"
        shutil.move(str(path), str(target))
        return f"trashed {rel} -> {target.relative_to(agent.root)}"

    def tool_move_file(self, args):
        agent = self.agent
        src = agent.path(args["src"])
        dst = agent.path(args["dst"])
        if not src.exists():
            raise ValueError("src does not exist")
        if dst.exists():
            raise ValueError("dst already exists")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"moved {src.relative_to(agent.root)} -> {dst.relative_to(agent.root)}"

    # ---- delegation -----------------------------------------------------

    def tool_delegate(self, args):
        agent = self.agent
        if agent.depth >= agent.max_depth:
            raise ValueError("delegate depth exceeded")
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        # Local import avoids a circular dependency at module load time.
        from .agent import MiniAgent
        child = MiniAgent(
            model_client=agent.model_client,
            workspace=agent.workspace,
            session_store=agent.session_store,
            approval_policy="auto",
            max_steps=int(args.get("max_steps", (agent.config.get("harness") or {}).get("delegate_max_steps", 100))),
            max_new_tokens=agent.max_new_tokens,
            depth=agent.depth + 1,
            max_depth=agent.max_depth,
            read_only=False,
            allowed_ops=agent.allowed_ops,
            sandbox=agent.sandbox,
            config=agent.config,
        )
        child.session["memory"]["task"] = task
        child.session["memory"]["notes"] = [agent.history_text()[:300]]
        return "delegate_result:\n" + child.ask(task)

    def tool_delegate_parallel(self, args):
        """Spawn multiple read-only child agents concurrently.

        Children share the model_client, but each gets its own session and
        memory. Results are aggregated as a JSON array. A failure in one
        task is reported as ``{"task": ..., "error": "..."}`` and does not
        cancel the rest.
        """
        agent = self.agent
        if agent.depth >= agent.max_depth:
            raise ValueError("delegate_parallel depth exceeded")
        tasks = args.get("tasks") or []
        if isinstance(tasks, str):
            # Best-effort: split a single string on newlines.
            tasks = [line.strip() for line in tasks.splitlines() if line.strip()]
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty list of strings")
        tasks = [str(item).strip() for item in tasks if str(item).strip()]
        if not tasks:
            raise ValueError("tasks must contain at least one non-empty entry")
        # Cap parallelism so we never blow up the upstream rate limit.
        harness = agent.config.get("harness", {})
        max_workers = int(harness.get("delegate_parallel_max_workers", 4))
        import json as _json
        import concurrent.futures
        from .agent import MiniAgent

        def _run_one(task_text):
            try:
                child = MiniAgent(
                    model_client=agent.model_client,
                    workspace=agent.workspace,
                    session_store=agent.session_store,
                    approval_policy="auto",
                    max_steps=int(args.get("max_steps", (agent.config.get("harness") or {}).get("delegate_max_steps", 100))),
                    max_new_tokens=agent.max_new_tokens,
                    depth=agent.depth + 1,
                    max_depth=agent.max_depth,
                    read_only=True,
                    allowed_ops=agent.allowed_ops,
                    sandbox=agent.sandbox,
                    config=agent.config,
                )
                child.session["memory"]["task"] = task_text
                return {"task": task_text, "result": child.ask(task_text)}
            except Exception as exc:  # noqa: BLE001 - we surface as data
                return {"task": task_text, "error": str(exc)}

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_run_one, t) for t in tasks]
            # Preserve input ordering for determinism.
            for fut in futures:
                results.append(fut.result())
        # Persist a snapshot of the parallel-delegation run for auditing.
        try:
            log_dir = agent.root / ".codelet" / "delegated"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{uuid.uuid4().hex[:8]}.json"
            log_path.write_text(_json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Logging is best-effort; never let it block the return value.
            pass
        return "delegate_parallel_result:\n" + _json.dumps(results, ensure_ascii=False, indent=2)

    # ---- net tools -----------------------------------------------------

    def tool_web_search(self, args):
        """Search the web using multiple backends: DDG Instant Answers → SearXNG → Perplexity (if key available)."""
        import os
        if os.environ.get("CODELET_NO_WEB_SEARCH"):
            return (
                "REFUSED: web_search is disabled for this task. "
                "You must use your training knowledge to complete the deliverable. "
                "Do NOT try to verify names, addresses, or details via web search. "
                "Create the deliverable using your existing knowledge and proceed immediately."
            )
        agent = self.agent
        query = str(args.get("query", "")).strip()
        max_results = min(int(args.get("max_results", 5)), 10)
        timeout = int((agent.config.get("harness") or {}).get("tool_timeout", 20))

        results = []

        # 1. Try DuckDuckGo Instant Answers API (fast, no API key, reliable for factual queries)
        results = self._search_ddg_instant(query, max_results, timeout)

        # 2. Fallback to SearXNG public instances (general web search, no API key)
        if not results:
            results = self._search_searxng(query, max_results, timeout)

        # 3. Try Perplexity via OpenRouter (requires OPENROUTER_API_KEY)
        if not results:
            results = self._search_perplexity_openrouter(query, max_results, timeout)

        if not results:
            return (
                "No results found. Web search is currently unavailable.\n"
                "This usually means:\n"
                "  • The query is not a well-known topic in DuckDuckGo's database\n"
                "  • Public search instances are temporarily down or rate-limited\n"
                "  • No search API key is configured\n\n"
                "To fix:\n"
                "  1. Set OPENROUTER_API_KEY in your environment for Perplexity search\n"
                "  2. Use web_fetch with a specific known URL instead\n"
                "  3. For well-known topics, rely on your training knowledge"
            )

        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            lines.append(f"   URL: {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
        return "\n".join(lines)

    def _search_searxng(self, query, max_results, timeout):
        """Search via public SearXNG instances (JSON API, no scraping)."""
        # Rotating list — these are community instances that expose the JSON API.
        # The list is ordered by historical reliability; dead instances are skipped.
        instances = [
            "https://search.sapti.me",
            "https://search.bus-hit.me",
            "https://search.demoniak.ch",
            "https://search.projectsegfault.com",
            "https://search.nordvedt.com",
            "https://search.rhscz.eu",
        ]
        for instance in instances:
            try:
                url = (
                    f"{instance}/search?"
                    + urllib.parse.urlencode(
                        {"q": query, "format": "json", "language": "en-US"}
                    )
                )
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36"
                    ),
                    "Accept": "application/json",
                }
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                results = []
                for r in data.get("results", [])[:max_results]:
                    results.append(
                        {
                            "url": r.get("url", ""),
                            "title": _re.sub(r"<[^>]+>", "", r.get("title", "")).strip(),
                            "snippet": _re.sub(
                                r"<[^>]+>", "", r.get("content", "")
                            ).strip(),
                        }
                    )
                if results:
                    return results
            except Exception:
                continue
        return []

    def _search_ddg_instant(self, query, max_results, timeout):
        """DuckDuckGo Instant Answers API — reliable for factual / well-known topics."""
        try:
            url = (
                "https://api.duckduckgo.com/?"
                + urllib.parse.urlencode(
                    {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1}
                )
            )
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                ),
                "Accept": "application/json",
            }
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            results = []
            # Main abstract
            abstract = data.get("Abstract", "").strip()
            abstract_url = data.get("AbstractURL", "").strip()
            if abstract and abstract_url:
                results.append(
                    {
                        "url": abstract_url,
                        "title": data.get("Heading", query),
                        "snippet": abstract,
                    }
                )
            # Related topics
            for topic in data.get("RelatedTopics", [])[:max_results]:
                if isinstance(topic, dict):
                    text = topic.get("Text", "").strip()
                    topic_url = topic.get("FirstURL", "").strip()
                    if text and topic_url:
                        results.append(
                            {
                                "url": topic_url,
                                "title": text.split(" - ")[0] if " - " in text else text[:60],
                                "snippet": text,
                            }
                        )
                if len(results) >= max_results:
                    break
            # Results array (rarely populated, but useful when it is)
            for r in data.get("Results", [])[:max_results]:
                if isinstance(r, dict):
                    text = r.get("Text", "").strip()
                    result_url = r.get("FirstURL", "").strip()
                    if text and result_url:
                        results.append(
                            {
                                "url": result_url,
                                "title": text.split(" - ")[0] if " - " in text else text[:60],
                                "snippet": text,
                            }
                        )
                if len(results) >= max_results:
                    break
            return results[:max_results]
        except Exception:
            return []

    def _search_perplexity_openrouter(self, query, max_results, timeout):
        """Perplexity Sonar via OpenRouter — requires OPENROUTER_API_KEY env var."""
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return []
        try:
            payload = json.dumps(
                {
                    "model": "perplexity/sonar",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a search assistant. Return a JSON array of search results. "
                                "Each result must have: title, url, snippet. "
                                f"Return at most {max_results} results."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"Search the web for: {query}",
                        },
                    ],
                    "max_tokens": 1024,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://codelet.local",
                    "X-Title": "Codelet",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            # Try to parse JSON array from the response
            try:
                results = json.loads(content)
                if isinstance(results, list):
                    return [
                        {
                            "url": r.get("url", r.get("link", "")),
                            "title": r.get("title", "")[:120],
                            "snippet": r.get("snippet", r.get("description", ""))[:300],
                        }
                        for r in results[:max_results]
                    ]
            except json.JSONDecodeError:
                # If the model didn't return valid JSON, treat the whole response as one result
                lines = [l.strip() for l in content.splitlines() if l.strip()]
                if lines:
                    return [
                        {
                            "url": "",
                            "title": lines[0][:120],
                            "snippet": " ".join(lines[1:])[:400],
                        }
                    ]
            return []
        except Exception:
            return []

    def tool_web_fetch(self, args):
        """Fetch a URL using a real headless browser (Playwright/Chromium).

        This executes JavaScript and bypasses most bot-detection (Bloomberg,
        Reuters, WSJ, etc.).  Falls back to plain urllib only when Playwright
        is not available.
        """
        import os
        if os.environ.get("CODELET_NO_WEB_SEARCH"):
            return (
                "REFUSED: web_fetch is disabled for this task. "
                "You must use your training knowledge to complete the deliverable. "
                "Do NOT try to verify information by fetching URLs. "
                "Create the deliverable using your existing knowledge and proceed immediately."
            )
        agent = self.agent
        url = str(args.get("url", "")).strip()
        max_chars = min(int(args.get("max_chars", 4000)), 16000)
        timeout_s = int((agent.config.get("harness") or {}).get("tool_timeout", 30))

        # Unwrap DuckDuckGo Lite redirector links (returned by tool_web_search).
        # Form: //duckduckgo.com/l/?uddg=<urlencoded-target>&rut=...
        if "duckduckgo.com/l/" in url and "uddg=" in url:
            if url.startswith("//"):
                url = "https:" + url
            try:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                if qs.get("uddg"):
                    url = urllib.parse.unquote(qs["uddg"][0])
            except Exception:
                pass
        elif url.startswith("//"):
            url = "https:" + url

        try:
            from playwright.sync_api import sync_playwright, Error as PWError
        except ImportError:
            PWError = None

        text = None

        if PWError is not None:
            # --- Playwright path (headless Chromium, renders JS, passes most bot checks) ---
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(
                        headless=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                        ],
                    )
                    ctx = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        java_script_enabled=True,
                        locale="en-US",
                        viewport={"width": 1280, "height": 800},
                        extra_http_headers={
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                    )
                    # Mask navigator.webdriver so bot-detection scripts can't see it.
                    ctx.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                    )
                    page = ctx.new_page()
                    # Block images/fonts to speed up load and save memory.
                    page.route(
                        "**/*",
                        lambda route: route.abort()
                        if route.request.resource_type in ("image", "media", "font")
                        else route.continue_(),
                    )
                    resp = page.goto(
                        url,
                        timeout=timeout_s * 1000,
                        wait_until="domcontentloaded",
                    )
                    # Wait a moment for JS-rendered content.
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    text = page.inner_text("body") or ""
                    browser.close()
                    # Detect Cloudflare / bot-challenge pages so the agent
                    # doesn't use the challenge text as real content.
                    _bot_signals = (
                        "detected unusual activity",
                        "please click the box below",
                        "verify you are human",
                        "enable javascript and cookies",
                        "checking your browser",
                        "ddos-guard",
                        "access denied",
                    )
                    low = text.lower()
                    if any(s in low for s in _bot_signals) and len(text) < 2000:
                        host = urllib.parse.urlparse(url).netloc
                        text = (
                            f"error: {host} serves a bot-detection CAPTCHA that "
                            "cannot be solved programmatically. "
                            "Do NOT retry this domain. "
                            "Use web_search to get news snippets about this topic instead."
                        )
            except PWError as e:
                # Playwright runtime error — fall through to urllib.
                text = None
            except Exception as e:
                text = None

        if text is None:
            # --- urllib fallback ---
            browser_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
            }
            req = urllib.request.Request(url, headers=browser_headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    raw = resp.read(1024 * 1024)
            except ssl.SSLError:
                # Retry with certificate verification disabled for sites with
                # self-signed or mismatched certificates (e.g., ir.weibo.com).
                try:
                    ctx = ssl._create_unverified_context()
                    with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as resp:
                        content_type = resp.headers.get("Content-Type", "")
                        raw = resp.read(1024 * 1024)
                except Exception as e:
                    return (
                        f"error: could not reach {url} due to SSL certificate issue: {e}. "
                        "Do NOT retry this URL. Use web_search snippets instead."
                    )
            except urllib.error.HTTPError as e:
                return (
                    f"error: {url} returned HTTP {e.code} {e.reason}. "
                    "This site blocks programmatic access. "
                    "Do NOT retry the same URL. Use web_search snippets instead."
                )
            except urllib.error.URLError as e:
                return f"error: could not reach {url}: {e.reason}"

            text = raw.decode("utf-8", errors="replace")
            if "html" in content_type.lower() or text.lstrip().startswith("<"):
                text = _re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text,
                               flags=_re.DOTALL | _re.IGNORECASE)
                text = _re.sub(r"<[^>]+>", " ", text)
                text = _html_module.unescape(text)

        # Normalise whitespace.
        text = "\n".join(
            line for line in
            (" ".join(chunk.split()) for chunk in text.splitlines())
            if line
        )
        return text[:max_chars]

    def tool_load_skill(self, args):
        """Return a discovered skill's body (progressive disclosure)."""
        from .skills import load_skill_body
        agent = self.agent
        name = str(args.get("name", "")).strip()
        if not name:
            raise ValueError("name must not be empty")
        return load_skill_body(getattr(agent, "skills", []) or [], name)

    def tool_remember_fact(self, args):
        """Append a one-line fact to the workspace repo-memory file."""
        agent = self.agent
        fact = str(args.get("fact", "")).strip()
        if not fact:
            raise ValueError("fact must not be empty")
        # Sanitise to a single line so the file remains a flat bullet list.
        flat = " ".join(fact.splitlines())[:500]
        target_dir = Path(agent.workspace.repo_root) / ".codelet"
        target_dir.mkdir(parents=True, exist_ok=True)
        memory_path = target_dir / "repo-memory.md"
        existing = memory_path.read_text(encoding="utf-8") if memory_path.is_file() else ""
        if not existing.endswith("\n") and existing:
            existing += "\n"
        memory_path.write_text(existing + f"- {flat}\n", encoding="utf-8")
        return f"remembered: {flat}"

    def tool_decompose(self, args):
        """Record a multi-step plan in the session for the current task.

        The caller may supply an explicit ``steps`` list.  When ``steps`` is
        absent or empty the goal string is auto-split on sentence boundaries
        (``". "`` separator) to produce the step list.
        """
        agent = self.agent
        goal = str(args.get("goal", "")).strip()
        if not goal:
            raise ValueError("goal must not be empty")
        steps = args.get("steps")
        if isinstance(steps, str):
            # Accept a newline-delimited string as a fallback.
            steps = [s.strip() for s in steps.splitlines() if s.strip()]
        if not steps:
            # Auto-split on ". " boundaries.
            parts = _re.split(r"\.\s+", goal.rstrip("."))
            steps = [p.strip() for p in parts if p.strip()]
            if not steps:
                steps = [goal]
        agent.session["plan"] = {"goal": goal, "steps": list(steps)}
        numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        return f"plan recorded\n{numbered}"


# Common alias keys the model frequently emits for a canonical schema field.
# Used by :func:`repair_tool_args` to coerce near-miss argument names so a
# small slip ("file" instead of "path") does not waste a whole turn.
_ARG_ALIASES = {
    "path": ("file", "filename", "filepath", "file_path", "fpath"),
    "command": ("cmd", "shell", "bash", "shell_command"),
    "content": ("text", "body", "data", "file_content"),
    "pattern": ("regex", "search_pattern"),
    "code": ("script", "python", "source"),
    "url": ("uri", "link", "href", "address"),
    "query": ("q", "search_query", "search", "keywords"),
    "task": ("prompt", "instruction", "subtask"),
    "tasks": ("subtasks", "prompts", "instructions"),
    "old_text": ("old", "find", "search_text", "from_text"),
    "new_text": ("new", "replace", "replacement", "to_text"),
    "src": ("source", "from", "src_path"),
    "dst": ("dest", "destination", "to", "target", "dst_path"),
    "name": ("skill", "skill_name"),
    "max_results": ("limit", "count", "num_results", "k"),
    "max_steps": ("steps", "budget"),
    "timeout": ("timeout_seconds", "timeout_s", "deadline"),
}


def _schema_type(spec):
    """Return the bare type name from a schema spec like ``"int=20"``."""
    return str(spec).split("=", 1)[0].strip()


def repair_tool_args(schema, args):
    """Best-effort coercion of model-supplied ``args`` toward ``schema``.

    Performs two non-destructive repairs and never raises:

    1. **Aliasing** — when a canonical schema key is missing but a well-known
       alias is present (e.g. ``file`` for ``path``), the alias is renamed.
    2. **Type coercion** — numeric strings/floats are coerced to ``int`` for
       ``int`` fields, and a bare string is wrapped/split into a list for
       ``list[str]`` fields (JSON arrays and comma-separated values supported).

    This mirrors the reference agent's tolerant schema handling so a small
    formatting slip does not burn a whole turn on an "invalid arguments" error.
    """
    if not isinstance(args, dict) or not isinstance(schema, dict):
        return args
    repaired = dict(args)
    for canonical, aliases in _ARG_ALIASES.items():
        if canonical in schema and canonical not in repaired:
            for alias in aliases:
                if alias in repaired:
                    repaired[canonical] = repaired.pop(alias)
                    break
    for key, spec in schema.items():
        if key not in repaired:
            continue
        typ = _schema_type(spec)
        val = repaired[key]
        try:
            if typ == "int" and not isinstance(val, bool):
                if isinstance(val, str) and val.strip():
                    repaired[key] = int(float(val.strip()))
                elif isinstance(val, float):
                    repaired[key] = int(val)
            elif typ == "list[str]":
                if isinstance(val, str):
                    text = val.strip()
                    parsed = None
                    if text.startswith("["):
                        try:
                            loaded = json.loads(text)
                        except Exception:
                            loaded = None
                        if isinstance(loaded, list):
                            parsed = [str(x) for x in loaded]
                    if parsed is None:
                        if not text:
                            parsed = []
                        elif "," in text:
                            parsed = [p.strip() for p in text.split(",") if p.strip()]
                        else:
                            parsed = [text]
                    repaired[key] = parsed
                elif isinstance(val, list):
                    repaired[key] = [str(x) for x in val]
        except Exception:
            pass
    return repaired


def tool_argument_validators(agent, name, args):
    """Validate `args` for the named tool. Raises ValueError on bad input."""
    args = args or {}

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            if not path.exists():
                top = sorted(p.name for p in agent.root.iterdir() if p.is_dir())[:12]
                raise ValueError(
                    f"path '{args.get('path')}' does not exist. "
                    f"Top-level directories: {', '.join(top)}"
                )
            raise ValueError("path is not a directory (it is a file)")
        return

    if name == "read_file":
        path = agent.path(args["path"])
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
        agent.path(args.get("path", "."))
        return

    if name == "glob":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        base = agent.path(args.get("path", "."))
        if not base.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        harness = agent.config.get("harness", {})
        max_timeout = int(harness.get("tool_max_timeout", 120))
        timeout = int(args.get("timeout", harness.get("tool_timeout", 20)))
        if timeout < 1 or timeout > max_timeout:
            raise ValueError(f"timeout must be in [1, {max_timeout}]")
        return

    if name == "write_file":
        path = agent.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", "")).replace("\r\n", "\n")
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        if agent.depth >= agent.max_depth:
            raise ValueError("delegate depth exceeded")
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        return

    if name == "delete_file":
        path = agent.path(args["path"])
        if not path.exists():
            raise ValueError("path does not exist")
        if path.is_dir() and any(path.iterdir()):
            raise ValueError("directory is not empty (refusing to recurse)")
        return

    if name == "move_file":
        src = agent.path(args["src"])
        dst = agent.path(args["dst"])
        if not src.exists():
            raise ValueError("src does not exist")
        if dst.exists():
            raise ValueError("dst already exists")
        return

    if name == "delegate_parallel":
        if agent.depth >= agent.max_depth:
            raise ValueError("delegate_parallel depth exceeded")
        tasks = args.get("tasks")
        if isinstance(tasks, str):
            tasks = [line.strip() for line in tasks.splitlines() if line.strip()]
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty list of strings")
        return

    if name == "load_skill":
        if not str(args.get("name", "")).strip():
            raise ValueError("name must not be empty")
        return

    if name == "remember_fact":
        if not str(args.get("fact", "")).strip():
            raise ValueError("fact must not be empty")
        return

    if name == "web_search":
        if not str(args.get("query", "")).strip():
            raise ValueError("query must not be empty")
        max_r = args.get("max_results", 5)
        if max_r is not None and (int(max_r) < 1 or int(max_r) > 10):
            raise ValueError("max_results must be between 1 and 10")
        return

    if name == "web_fetch":
        url = str(args.get("url", "")).strip()
        if not url:
            raise ValueError("url must not be empty")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return

    if name == "run_python":
        code = str(args.get("code", "")).strip()
        if not code:
            # Give the model an actionable diagnosis instead of a bare error.
            if args.get("path") or args.get("content"):
                raise ValueError(
                    "run_python requires {\"code\": \"...\"}, not path/content. "
                    "You used write_file's argument shape. "
                    "For long scripts: first use write_file to save the script, "
                    "then use run_shell {\"command\": \"python script.py\"} to run it."
                )
            if args.get("args"):
                raise ValueError(
                    "run_python requires a top-level \"code\" key, not a nested \"args\" key. "
                    "Correct form: {\"name\":\"run_python\",\"args\":{\"code\":\"...\",\"timeout\":30}}"
                )
            raise ValueError("code must not be empty")
        harness = agent.config.get("harness", {})
        max_timeout = int(harness.get("tool_max_timeout", 120))
        timeout = int(args.get("timeout", harness.get("tool_timeout", 20)))
        if timeout < 1 or timeout > max_timeout:
            raise ValueError(f"timeout must be in [1, {max_timeout}]")
        return
