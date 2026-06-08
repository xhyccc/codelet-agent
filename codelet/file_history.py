"""File history snapshots for codelet.

Mirrors the reference agent's fileHistory.ts:
- Create snapshots before writes
- Rewind files to previous snapshots
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional


def _history_dir(workspace_root: str) -> Path:
    return Path(workspace_root) / ".codelet" / "file_history"


def _snapshot_path(workspace_root: str, file_path: str) -> Path:
    # Hash the file path to create a unique snapshot directory
    safe_name = file_path.replace("/", "_").replace("\\", "_")
    return _history_dir(workspace_root) / f"{safe_name}.jsonl"


def create_snapshot(workspace_root: str, file_path: str) -> bool:
    """Create a snapshot of a file before it is modified."""
    full_path = Path(workspace_root) / file_path
    if not full_path.is_file():
        return False
    snapshot_path = _snapshot_path(workspace_root, file_path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    content = full_path.read_text(encoding="utf-8", errors="replace")
    entry = {
        "timestamp": int(time.time() * 1000),
        "content": content,
    }
    with open(snapshot_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return True


def get_file_history(workspace_root: str, file_path: str) -> List[dict]:
    """Get all snapshots for a file."""
    snapshot_path = _snapshot_path(workspace_root, file_path)
    if not snapshot_path.is_file():
        return []
    entries = []
    with open(snapshot_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def rewind_file(workspace_root: str, file_path: str, steps: int = 1) -> bool:
    """Restore a file to a previous snapshot."""
    entries = get_file_history(workspace_root, file_path)
    if not entries:
        return False
    target = entries[-steps] if steps <= len(entries) else entries[0]
    full_path = Path(workspace_root) / file_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(target["content"], encoding="utf-8")
    return True
