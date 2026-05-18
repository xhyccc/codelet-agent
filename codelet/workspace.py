"""Workspace context collection.

Snapshots the user's repo on agent startup so the prompt prefix can include
stable facts (cwd, branch, status, recent commits, project docs). This module
intentionally has no LLM dependencies.
"""

import subprocess
from datetime import datetime
from pathlib import Path

from .utils import DOC_NAMES, clip


class WorkspaceContext:
    """A snapshot of the user's working directory and git state."""

    def __init__(self, cwd, repo_root, branch, default_branch, status, recent_commits, project_docs, captured_at="", timezone_name=""):
        self.cwd = cwd
        self.repo_root = repo_root
        self.branch = branch
        self.default_branch = default_branch
        self.status = status
        self.recent_commits = recent_commits
        self.project_docs = project_docs
        self.captured_at = captured_at
        self.timezone_name = timezone_name

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

        now_local = datetime.now().astimezone()
        captured_at = now_local.strftime("%Y-%m-%d %H:%M:%S %Z")
        timezone_name = now_local.tzname() or ""

        return cls(
            cwd=str(cwd),
            repo_root=str(repo_root),
            branch=git(["branch", "--show-current"], "-") or "-",
            default_branch=(git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], "origin/main") or "origin/main").removeprefix("origin/"),
            status=clip(git(["status", "--short"], "clean") or "clean", 1500),
            recent_commits=[line for line in git(["log", "--oneline", "-5"]).splitlines() if line],
            project_docs=docs,
            captured_at=captured_at,
            timezone_name=timezone_name,
        )

    def text(self):
        """Return the workspace snapshot as a flush-left text block.

        The leading ``Workspace:`` label and field names are preserved so
        existing tests and downstream tools keep working when the prompt
        builder wraps this block in an ``<workspace>`` XML tag.
        """
        commits = "\n".join(f"- {line}" for line in self.recent_commits) or "- none"
        docs = "\n".join(f"- {path}\n{snippet}" for path, snippet in self.project_docs.items()) or "- none"
        lines = [
            "Workspace:",
            f"- time: {self.captured_at}",
            f"- timezone: {self.timezone_name}",
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
        ]
        return "\n".join(lines)
