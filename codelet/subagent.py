"""Subagent context for codelet.

Mirrors the reference agent's createSubagentContext:
- Inherit parent's permission context and file cache
- Shared setAppState for tasks
"""

from __future__ import annotations

from typing import Any, Dict


def create_subagent_context(agent) -> Dict[str, Any]:
    """Create a subagent context that inherits the parent's state.

    Returns a dict with:
    - permission_context: the parent's permission context
    - read_file_state: the parent's file read cache
    - set_app_state_for_tasks: a no-op callable (placeholder for shared state)
    """
    return {
        "permission_context": agent.permission_context,
        "read_file_state": agent._file_read_cache,
        "set_app_state_for_tasks": lambda f: None,
    }
