"""Session-baseline verification.

The Claude Code architecture brief recommends that every custom-agent
execution session begin with a verification baseline against the physical
repository state. This prevents compounding hallucinations and architectural
drift across multiple, disjointed agent runs.

The baseline captured here is intentionally cheap: it records the workspace
root, branch, latest commit, status digest, and the modification time + size
of any tracked memory files. On a subsequent session the new baseline is
compared against the one stored in the durable session and any drift is
returned as a list of human-readable strings so the agent can surface it to
the model or the user.

This module has no LLM dependencies and runs entirely on the local filesystem
plus ``git`` (when available).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _git(args, cwd):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _file_signature(path):
    """Return a deterministic signature for a file (size + sha256 prefix)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(data).hexdigest()[:16]
    return {"size": len(data), "sha256_16": digest}


def capture_baseline(repo_root, *, watch_files=None):
    """Capture the physical repository state for the current session.

    Parameters
    ----------
    repo_root : str | Path
        Path to the workspace root.
    watch_files : iterable[str] | None
        Workspace-relative paths whose ``size`` and short sha256 are recorded
        in addition to the git facts. Files that don't exist are recorded as
        ``None`` so :func:`diff_baseline` can detect creation/deletion.

    Returns a JSON-serialisable dict.
    """
    repo_root = Path(repo_root).resolve()
    watch_files = list(watch_files or [])
    files = {}
    for rel in watch_files:
        target = repo_root / rel
        files[rel] = _file_signature(target) if target.exists() else None
    return {
        "repo_root": str(repo_root),
        "branch": _git(["branch", "--show-current"], repo_root) or "-",
        "head_commit": _git(["rev-parse", "HEAD"], repo_root) or "",
        "status_digest": hashlib.sha256(
            _git(["status", "--porcelain"], repo_root).encode("utf-8")
        ).hexdigest()[:16],
        "files": files,
    }


def diff_baseline(previous, current):
    """Return a list of human-readable drift messages.

    ``previous`` and ``current`` are dicts produced by :func:`capture_baseline`.
    A missing ``previous`` returns an empty list (first run is never drifted).
    """
    if not previous:
        return []
    drift = []
    if previous.get("repo_root") != current.get("repo_root"):
        drift.append(
            "repo_root changed: "
            f"{previous.get('repo_root')!r} -> {current.get('repo_root')!r}"
        )
    if previous.get("branch") != current.get("branch"):
        drift.append(
            f"branch changed: {previous.get('branch')!r} -> {current.get('branch')!r}"
        )
    if previous.get("head_commit") != current.get("head_commit"):
        drift.append(
            "HEAD moved: "
            f"{previous.get('head_commit', '')[:12]} -> {current.get('head_commit', '')[:12]}"
        )
    if previous.get("status_digest") != current.get("status_digest"):
        drift.append("working tree status differs from previous session")
    prev_files = previous.get("files") or {}
    curr_files = current.get("files") or {}
    for rel in sorted(set(prev_files) | set(curr_files)):
        before = prev_files.get(rel)
        after = curr_files.get(rel)
        if before is None and after is not None:
            drift.append(f"file appeared: {rel}")
        elif before is not None and after is None:
            drift.append(f"file disappeared: {rel}")
        elif before != after:
            drift.append(f"file changed on disk: {rel}")
    return drift


def verify_session_baseline(session, repo_root, *, watch_files=None):
    """Update ``session['baseline']`` and return the drift list.

    The function mutates ``session`` so the new baseline is persisted on the
    next save. If no previous baseline was recorded it returns ``[]`` and
    seeds the first one.
    """
    current = capture_baseline(repo_root, watch_files=watch_files)
    previous = session.get("baseline")
    drift = diff_baseline(previous, current)
    session["baseline"] = current
    return drift
