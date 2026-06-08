"""Global persistent history for codelet.

Mirrors the reference agent's history.ts:
- Append every user prompt to a global history.jsonl
- Store paste content by hash for large inputs
- Deduplicate entries by display text
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Dict, List, Optional


def _history_path(workspace_root: str) -> Path:
    return Path(workspace_root) / ".codelet" / "history.jsonl"


def _paste_store_path(workspace_root: str) -> Path:
    return Path(workspace_root) / ".codelet" / "paste_store.json"


def hash_pasted_text(text: str) -> str:
    """Hash large pasted text for external storage."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def store_pasted_text(workspace_root: str, text_hash: str, content: str) -> None:
    """Store pasted text by hash."""
    path = _paste_store_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    data[text_hash] = content
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def retrieve_pasted_text(workspace_root: str, text_hash: str) -> Optional[str]:
    """Retrieve pasted text by hash."""
    path = _paste_store_path(workspace_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(text_hash)
    except (json.JSONDecodeError, OSError):
        return None


def append_history(
    workspace_root: str,
    display: str,
    session_id: str,
    project: str,
    pasted_contents: Optional[Dict[int, dict]] = None,
) -> None:
    """Append a single entry to the global history.jsonl."""
    path = _history_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "display": display,
        "timestamp": int(time.time() * 1000),
        "project": project,
        "sessionId": session_id,
        "pastedContents": pasted_contents or {},
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def read_history(workspace_root: str, max_items: int = 100) -> List[dict]:
    """Read history entries newest-first."""
    path = _history_path(workspace_root)
    if not path.is_file():
        return []
    entries: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    # Deduplicate by display text, newest first
    seen: set = set()
    result: List[dict] = []
    for entry in reversed(entries):
        display = entry.get("display", "")
        if display in seen:
            continue
        seen.add(display)
        result.append(entry)
        if len(result) >= max_items:
            break
    return result


def get_history_for_project(workspace_root: str, project: str, max_items: int = 100) -> List[dict]:
    """Get history entries for a specific project."""
    all_entries = read_history(workspace_root, max_items=max_items * 2)
    return [e for e in all_entries if e.get("project") == project][:max_items]
