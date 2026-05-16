"""Hardening goodies for the mini-coding-agent.

This module bundles three small, optional countermeasures inspired by
patterns observed in production coding agents:

* **Decoy tools** -- inject fake tool entries into the prompt so an
  attacker (or a careless distiller) sees a tool surface that does not
  actually exist.  Calls to a decoy are refused at runtime.
* **YOLO command classifier** -- a small regex-based safelist of
  obviously safe shell commands (``ls``, ``pwd``, ``cat``, ``grep`` ...)
  so the ``ask`` approval policy can auto-approve trivially-safe
  invocations without nagging the user.
* **Undercover identity** -- when the ``MINI_AGENT_UNDERCOVER`` env var
  is set, swap the agent identity for a generic "helpful assistant"
  string and suppress the welcome banner.  Useful for benchmark / eval
  runs where you do not want the model to recognise the harness.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Dict, Iterable, List


# ---------------------------------------------------------------------------
# Decoy tools
# ---------------------------------------------------------------------------


def _decoy_run(name: str):
    def _runner(_args):
        return (
            f"error: tool {name} is unavailable in this environment "
            "(refused by safety policy)."
        )
    return _runner


DEFAULT_DECOY_TOOLS = (
    {
        "name": "secret_eval",
        "schema": {"code": "str"},
        "description": "Run privileged evaluation code (admin only).",
    },
    {
        "name": "network_probe",
        "schema": {"target": "str", "port": "int"},
        "description": "Probe a network target for open ports.",
    },
    {
        "name": "exfiltrate",
        "schema": {"path": "str", "dest": "str"},
        "description": "Upload a workspace file to a remote endpoint.",
    },
)


def apply_decoy_tools(tools: Dict[str, dict], specs: Iterable[dict] = ()) -> Dict[str, dict]:
    """Add decoy entries to ``tools`` in place and return it.

    Each spec is ``{"name": str, "schema": dict, "description": str}``.
    If ``specs`` is empty we install :data:`DEFAULT_DECOY_TOOLS`.
    """
    specs = list(specs) or list(DEFAULT_DECOY_TOOLS)
    for spec in specs:
        name = spec.get("name")
        if not name or name in tools:
            continue
        tools[name] = {
            "schema": dict(spec.get("schema") or {}),
            "risky": True,
            "description": str(spec.get("description") or "Unavailable tool."),
            "run": _decoy_run(name),
            "_decoy": True,
        }
    return tools


def is_decoy(tool_spec: dict) -> bool:
    return bool(tool_spec.get("_decoy"))


# ---------------------------------------------------------------------------
# YOLO command classifier
# ---------------------------------------------------------------------------


# Each entry is "argv[0]" -> regex for the WHOLE command after shlex.join.
_YOLO_SAFE: Dict[str, re.Pattern] = {
    "ls":     re.compile(r"^ls(?:\s+-[A-Za-z]+)?(?:\s+[\w./\-]+)*$"),
    "pwd":    re.compile(r"^pwd$"),
    "echo":   re.compile(r"^echo(?:\s+\S+)*$"),
    "cat":    re.compile(r"^cat\s+[\w./\-]+(?:\s+[\w./\-]+)*$"),
    "head":   re.compile(r"^head(?:\s+-n\s*\d+)?\s+[\w./\-]+$"),
    "tail":   re.compile(r"^tail(?:\s+-n\s*\d+)?\s+[\w./\-]+$"),
    "wc":     re.compile(r"^wc(?:\s+-[lwcm]+)?\s+[\w./\-]+$"),
    "stat":   re.compile(r"^stat\s+[\w./\-]+$"),
    "file":   re.compile(r"^file\s+[\w./\-]+$"),
    "uname":  re.compile(r"^uname(?:\s+-[arn])?$"),
    "whoami": re.compile(r"^whoami$"),
    "date":   re.compile(r"^date$"),
    # git read-only inspection
    "git":    re.compile(r"^git\s+(?:status|log|diff|branch|show|remote|rev-parse|ls-files)(?:\s+[\w./\-:^~]+)*$"),
    # python -V, --version queries
    "python": re.compile(r"^python(?:3)?\s+(?:-V|--version)$"),
}


# Characters that would let the command escape the safelist.
_UNSAFE_CHARS_RE = re.compile(r"[;&|`$><\\]")


def is_safe_command(command: str) -> bool:
    """Conservative classifier: return True only for *known* safe commands.

    The bar is intentionally low coverage / high precision: we want zero
    false positives.  Anything with shell metacharacters, pipes,
    redirection, command substitution, or backslash escapes is rejected
    outright.
    """
    if not isinstance(command, str):
        return False
    cmd = command.strip()
    if not cmd or _UNSAFE_CHARS_RE.search(cmd):
        return False
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts:
        return False
    head = parts[0]
    pattern = _YOLO_SAFE.get(head)
    if pattern is None:
        return False
    # Normalise whitespace before regex match.
    normalised = " ".join(parts)
    return bool(pattern.fullmatch(normalised))


# ---------------------------------------------------------------------------
# Undercover identity
# ---------------------------------------------------------------------------


UNDERCOVER_IDENTITY = (
    "You are a helpful assistant. Be direct and concise. "
    "Follow instructions, ask clarifying questions when needed."
)


def undercover_enabled(env: Dict[str, str] | None = None) -> bool:
    env = env if env is not None else os.environ
    value = str(env.get("MINI_AGENT_UNDERCOVER", "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def apply_undercover_identity(prompts_cfg: dict) -> dict:
    """Return a *new* prompts_cfg with the identity layer replaced."""
    out = dict(prompts_cfg or {})
    out["agent_identity"] = UNDERCOVER_IDENTITY
    return out
