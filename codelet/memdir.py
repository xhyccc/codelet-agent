"""Structured memory directory (memdir) for codelet.

Mirrors the reference agent's memdir/memdir.ts:
- MEMORY.md with typed taxonomy (user, feedback, project, reference)
- Frontmatter parsing for type annotations
- Nested memory attachment for subdir CLAUDE.md files
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional


_FRONT_MATTER_RE = re.compile(r"---\n(.*?)\n---\n", re.DOTALL)


def _parse_front_matter(text: str) -> Dict[str, str]:
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}
    out: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def ensure_memory_dir(workspace_root: str) -> Path:
    """Ensure .codelet/MEMORY.md exists."""
    path = Path(workspace_root) / ".codelet" / "MEMORY.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(
            "---\n"
            "type: project\n"
            "---\n"
            "# Project Memory\n\n"
            "Add project-specific notes here.\n",
            encoding="utf-8",
        )
    return path


def load_memory_prompt(workspace_root: str) -> Dict[str, str]:
    """Load MEMORY.md and return typed sections.

    Returns a dict mapping type -> content for each frontmatter-typed block.
    """
    path = Path(workspace_root) / ".codelet" / "MEMORY.md"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    sections: Dict[str, str] = {}
    # Find all frontmatter blocks and their following body
    cursor = 0
    while True:
        m = _FRONT_MATTER_RE.search(text, cursor)
        if not m:
            break
        fm = _parse_front_matter(m.group(0))
        body_start = m.end()
        # Look for the next frontmatter block
        next_m = _FRONT_MATTER_RE.search(text, body_start)
        body_end = next_m.start() if next_m else len(text)
        body = text[body_start:body_end]
        section_type = fm.get("type", "project")
        sections[section_type] = body.strip()
        cursor = body_end
    return sections


def attach_nested_memory(agent, subdir_path: str) -> str:
    """Attach nested memory from a subdirectory's CLAUDE.md.

    Returns the memory text if found, empty string otherwise.
    """
    subdir = Path(subdir_path)
    if not subdir.is_dir():
        subdir = subdir.parent
    for fname in ("CLAUDE.md", "AGENT.md", "AGENTS.md"):
        candidate = subdir / fname
        if candidate.is_file():
            try:
                body = candidate.read_text(encoding="utf-8", errors="replace")
                return body.strip()
            except OSError:
                continue
    return ""
