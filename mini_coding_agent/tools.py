"""Tool implementations and the per-agent tool registry.

The :class:`ToolRegistry` builds the dictionary of available tools based on the
agent's permissions (``allowed_ops``) and delegation budget. Each tool is a
small Python callable bound to a host :class:`~mini_coding_agent.agent.MiniAgent`
so it can access the workspace and sandbox configuration.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from difflib import unified_diff
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
                    "(.mini-coding-agent/trash/<session-id>/). Reversible."
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
            tools["delegate"] = {
                "schema": {"task": "str", "max_steps": "int=3"},
                "risky": False,
                "description": "Ask a bounded read-only child agent to investigate.",
                "run": self.tool_delegate,
            }
            tools["delegate_parallel"] = {
                "schema": {"tasks": "list[str]", "max_steps": "int=3"},
                "risky": False,
                "description": (
                    "Run multiple bounded read-only child agents in parallel. "
                    "Each task is a string; returns a JSON list of {task, result}."
                ),
                "run": self.tool_delegate_parallel,
            }
        tools["decompose"] = {
            "schema": {"goal": "str"},
            "risky": False,
            "description": (
                "Record a plan that breaks the user's goal into ordered steps. "
                "Pass a goal string; returns an acknowledgement and saves the plan "
                "to session['plan']."
            ),
            "run": self.tool_decompose,
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
                    "Append a one-line fact to .mini-coding-agent/repo-memory.md "
                    "so future sessions inherit it."
                ),
                "run": self.tool_remember_fact,
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
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
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
            for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                if pattern.lower() in line.lower():
                    matches.append(f"{file_path.relative_to(agent.root)}:{number}:{line}")
                    if len(matches) >= 200:
                        return "\n".join(matches)
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
            result = subprocess.run(
                argv,
                cwd=agent.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                **{k: v for k, v in sandbox_kwargs.items() if k != "preexec_fn"},
            )
        else:
            result = subprocess.run(
                command,
                cwd=agent.root,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                **sandbox_kwargs,
            )
        tool_out_cfg = (harness.get("tool_output") or {})
        out_limit = int(tool_out_cfg.get("max_chars", harness.get("max_tool_output", 4000)))
        return "\n".join(
            [
                f"exit_code: {result.returncode}",
                "stdout:",
                _scrub_subprocess_text(result.stdout, limit=out_limit).strip() or "(empty)",
                "stderr:",
                _scrub_subprocess_text(result.stderr, limit=out_limit).strip() or "(empty)",
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

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as fh:
            fh.write(code)
            tmp_path = fh.name
        try:
            result = subprocess.run(
                [sys.executable, tmp_path],
                cwd=agent.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                **sandbox_kwargs,
            )
        finally:
            os.unlink(tmp_path)
        tool_out_cfg = (harness.get("tool_output") or {})
        out_limit = int(tool_out_cfg.get("max_chars", harness.get("max_tool_output", 4000)))
        return "\n".join(
            [
                f"exit_code: {result.returncode}",
                "stdout:",
                _scrub_subprocess_text(result.stdout, limit=out_limit).strip() or "(empty)",
                "stderr:",
                _scrub_subprocess_text(result.stderr, limit=out_limit).strip() or "(empty)",
            ]
        )

    # ---- write tools ----------------------------------------------------

    def tool_write_file(self, args):
        agent = self.agent
        path = agent.path(args["path"])
        content = str(args["content"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {path.relative_to(agent.root)} ({len(content)} chars)"

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
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        new_text = str(args["new_text"])
        updated = text.replace(old_text, new_text, 1)
        path.write_text(updated, encoding="utf-8")
        rel = path.relative_to(agent.root)
        diff = _render_diff(text, updated, str(rel))
        return f"patched {rel}\n{diff}" if diff else f"patched {rel}"

    def _trash_dir(self):
        agent = self.agent
        session_id = (agent.session or {}).get("id", "unknown")
        trash = agent.root / ".mini-coding-agent" / "trash" / session_id
        trash.mkdir(parents=True, exist_ok=True)
        return trash

    def tool_delete_file(self, args):
        agent = self.agent
        path = agent.path(args["path"])
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
            approval_policy="never",
            max_steps=int(args.get("max_steps", 3)),
            max_new_tokens=agent.max_new_tokens,
            depth=agent.depth + 1,
            max_depth=agent.max_depth,
            read_only=True,
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
                    approval_policy="never",
                    max_steps=int(args.get("max_steps", 3)),
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
            log_dir = agent.root / ".mini-coding-agent" / "delegated"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{uuid.uuid4().hex[:8]}.json"
            log_path.write_text(_json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Logging is best-effort; never let it block the return value.
            pass
        return "delegate_parallel_result:\n" + _json.dumps(results, ensure_ascii=False, indent=2)

    def tool_decompose(self, args):
        """Persist a plan that breaks the user's goal into ordered steps."""
        agent = self.agent
        goal = str(args.get("goal", "")).strip()
        if not goal:
            raise ValueError("goal must not be empty")
        steps = args.get("steps")
        if isinstance(steps, str):
            steps = [line.strip(" -*\t") for line in steps.splitlines() if line.strip()]
        if not isinstance(steps, list):
            # Auto-decompose: a deterministic fallback so the tool is always
            # usable even when the model only supplies a goal. Heuristic:
            # split on sentences / explicit numbering. The agent is expected
            # to refine the plan through subsequent calls.
            import re as _re
            chunks = _re.split(r"(?:\n+|\s*(?:->|=>|;|\.\s))", goal)
            steps = [chunk.strip() for chunk in chunks if chunk.strip()]
            if len(steps) < 2:
                steps = [goal]
        agent.session["plan"] = {"goal": goal, "steps": steps}
        agent.session_path = agent.session_store.save(agent.session)
        bullet_lines = "\n".join(f"  {idx + 1}. {step}" for idx, step in enumerate(steps))
        return f"plan recorded for goal: {goal}\n{bullet_lines}"

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
        target_dir = Path(agent.workspace.repo_root) / ".mini-coding-agent"
        target_dir.mkdir(parents=True, exist_ok=True)
        memory_path = target_dir / "repo-memory.md"
        existing = memory_path.read_text(encoding="utf-8") if memory_path.is_file() else ""
        if not existing.endswith("\n") and existing:
            existing += "\n"
        memory_path.write_text(existing + f"- {flat}\n", encoding="utf-8")
        return f"remembered: {flat}"


def tool_argument_validators(agent, name, args):
    """Validate `args` for the named tool. Raises ValueError on bad input."""
    args = args or {}

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
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

    if name == "decompose":
        goal = str(args.get("goal", "")).strip()
        if not goal:
            raise ValueError("goal must not be empty")
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

    if name == "run_python":
        code = str(args.get("code", "")).strip()
        if not code:
            raise ValueError("code must not be empty")
        harness = agent.config.get("harness", {})
        max_timeout = int(harness.get("tool_max_timeout", 120))
        timeout = int(args.get("timeout", harness.get("tool_timeout", 20)))
        if timeout < 1 or timeout > max_timeout:
            raise ValueError(f"timeout must be in [1, {max_timeout}]")
        return
