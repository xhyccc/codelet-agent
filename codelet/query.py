"""Query engine utilities for codelet.

Mirrors the reference agent's query.ts:
- Token budget tracking during prompt assembly
- Context window awareness per model
- Streaming tool executor with progress events
"""

from __future__ import annotations

from typing import Dict, Generator, Optional


# Approximate context windows for common models
MODEL_CONTEXT_WINDOWS: Dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3-opus": 200_000,
    "kimi": 200_000,
    "moonshot": 200_000,
    "glm-4": 128_000,
    "zhipu": 128_000,
    "deepseek-chat": 64_000,
    "deepseek-coder": 64_000,
    "default": 128_000,
}


def get_context_window(model_name: str) -> int:
    """Return the context window size for a model name."""
    name_lower = (model_name or "").lower()
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if key in name_lower:
            return window
    return MODEL_CONTEXT_WINDOWS["default"]


def estimate_tokens(text: str) -> int:
    """Rough token count estimate (4 chars per token)."""
    return max(1, len(text) // 4)


class ContentReplacementState:
    """Tracks aggregate tool result budget across turns."""

    def __init__(self):
        self.total_replaced = 0
        self.replacement_log: List[dict] = []


def apply_aggregate_budget(
    history: List[dict],
    budget: int,
    state: Optional[ContentReplacementState] = None,
) -> List[dict]:
    """Apply an aggregate budget to tool results in history.

    Returns a deep copy of history with over-budget tool outputs truncated.
    """
    if state is None:
        state = ContentReplacementState()
    result = []
    for item in history:
        copied = dict(item)
        if copied.get("role") == "tool":
            content = copied.get("content", "")
            if isinstance(content, str) and len(content) > budget:
                copied["content"] = (
                    content[:budget].rstrip()
                    + f" ... [aggregate budget: {len(content) - budget} chars dropped]"
                )
                state.total_replaced += len(content) - budget
                state.replacement_log.append({
                    "tool": copied.get("name"),
                    "original": len(content),
                    "budget": budget,
                })
        result.append(copied)
    return result


class StreamingToolExecutor:
    """Execute a tool with streaming progress events."""

    def __init__(self, agent):
        self.agent = agent

    def execute(self, tool_name: str, args: dict):
        """Yield progress events during tool execution."""
        yield {"type": "progress", "message": f"Starting {tool_name}..."}
        result = self.agent.run_tool(tool_name, args)
        yield {"type": "progress", "message": f"Finished {tool_name}"}
        yield {"type": "result", "content": result}
