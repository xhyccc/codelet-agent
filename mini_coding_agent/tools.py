"""Tool implementations and the per-agent tool registry.

The :class:`ToolRegistry` builds the dictionary of available tools based on the
agent's permissions (``allowed_ops``) and delegation budget. Each tool is a
small Python callable bound to a host :class:`~mini_coding_agent.agent.MiniAgent`
so it can access the workspace and sandbox configuration.
"""

import os
import shutil
import subprocess
import sys
import tempfile

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
        path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
        return f"patched {path.relative_to(agent.root)}"

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
