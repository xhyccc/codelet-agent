"""Cost and token usage tracking for codelet.

Mirrors the reference agent's cost-tracker.ts functionality:
- Accumulate per-session API cost and token usage
- Track per-model usage breakdown
- Enforce budget limits
- Persist cost state to project config
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    web_search_requests: int = 0


@dataclass
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    web_search_requests: int = 0
    cost_usd: float = 0.0
    context_window: int = 0
    max_output_tokens: int = 0


@dataclass
class CostState:
    total_cost_usd: float = 0.0
    total_api_duration_ms: float = 0.0
    total_tool_duration_ms: float = 0.0
    total_lines_added: int = 0
    total_lines_removed: int = 0
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    model_usage: Dict[str, ModelUsage] = field(default_factory=dict)


# Rough cost per 1M tokens (USD) for common models.
# These are approximate and can be overridden via config.
DEFAULT_MODEL_COSTS: Dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.00, 30.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-haiku": (0.25, 1.25),
    "claude-3-opus": (15.00, 75.00),
    "kimi": (0.50, 2.00),
    "moonshot": (0.50, 2.00),
    "glm-4": (0.50, 2.00),
    "zhipu": (0.50, 2.00),
    "deepseek-chat": (0.14, 0.28),
    "deepseek-coder": (0.14, 0.28),
    "default": (0.50, 2.00),
}


def _get_model_costs(model_name: str) -> tuple[float, float]:
    """Return (input_cost_per_1m, output_cost_per_1m) for a model name."""
    name_lower = (model_name or "").lower()
    for key, costs in DEFAULT_MODEL_COSTS.items():
        if key in name_lower:
            return costs
    return DEFAULT_MODEL_COSTS["default"]


def estimate_cost(
    model_name: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> float:
    """Estimate API cost in USD for a model call."""
    input_cost, output_cost = _get_model_costs(model_name)
    # Cache read is typically cheaper (we use 10% of input cost as a rough estimate)
    cache_read_cost = input_cost * 0.1
    # Cache creation is same as input cost
    cache_creation_cost = input_cost

    cost = (
        (input_tokens / 1_000_000) * input_cost
        + (output_tokens / 1_000_000) * output_cost
        + (cache_read_input_tokens / 1_000_000) * cache_read_cost
        + (cache_creation_input_tokens / 1_000_000) * cache_creation_cost
    )
    return round(cost, 6)


class CostTracker:
    """Tracks accumulated cost and token usage for a session."""

    def __init__(self, model_name: str = "unknown"):
        self.state = CostState()
        self.model_name = model_name
        self._has_unknown_model_cost = False

    def record_call(
        self,
        model_name: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        web_search_requests: int = 0,
        api_duration_ms: float = 0.0,
    ) -> float:
        """Record a single API call and return its estimated cost."""
        # Sanitize model_name in case a MagicMock slips through
        model_name = str(model_name) if model_name else "unknown"
        if "MagicMock" in model_name:
            model_name = "unknown"
        cost = estimate_cost(
            model_name,
            input_tokens,
            output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
        )

        self.state.total_cost_usd += cost
        self.state.total_api_duration_ms += api_duration_ms

        # Update aggregate token usage
        self.state.token_usage.input_tokens += input_tokens
        self.state.token_usage.output_tokens += output_tokens
        self.state.token_usage.cache_read_input_tokens += cache_read_input_tokens
        self.state.token_usage.cache_creation_input_tokens += cache_creation_input_tokens
        self.state.token_usage.web_search_requests += web_search_requests

        # Update per-model usage
        usage = self.state.model_usage.setdefault(
            model_name,
            ModelUsage(),
        )
        usage.input_tokens += input_tokens
        usage.output_tokens += output_tokens
        usage.cache_read_input_tokens += cache_read_input_tokens
        usage.cache_creation_input_tokens += cache_creation_input_tokens
        usage.web_search_requests += web_search_requests
        usage.cost_usd += cost

        return cost

    def record_tool_duration(self, duration_ms: float) -> None:
        """Record time spent executing tools."""
        self.state.total_tool_duration_ms += duration_ms

    def record_lines_changed(self, added: int = 0, removed: int = 0) -> None:
        """Record lines of code changed."""
        self.state.total_lines_added += added
        self.state.total_lines_removed += removed

    def check_budget(self, max_budget_usd: Optional[float]) -> bool:
        """Return True if the budget is exceeded (or no budget is set)."""
        if max_budget_usd is None or max_budget_usd <= 0:
            return False
        return self.state.total_cost_usd >= max_budget_usd

    def format_summary(self) -> str:
        """Format a human-readable cost summary."""
        lines = [
            f"Total cost:            ${self.state.total_cost_usd:.4f}",
            f"Total duration (API):  {self.state.total_api_duration_ms / 1000:.1f}s",
            f"Total code changes:    {self.state.total_lines_added} lines added, {self.state.total_lines_removed} lines removed",
            "Usage by model:",
        ]
        for model, usage in self.state.model_usage.items():
            lines.append(
                f"  {model}: {usage.input_tokens} input, {usage.output_tokens} output, "
                f"{usage.cache_read_input_tokens} cache read, {usage.cache_creation_input_tokens} cache write"
                f" (${usage.cost_usd:.4f})"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize state to a plain dict."""
        return {
            "total_cost_usd": self.state.total_cost_usd,
            "total_api_duration_ms": self.state.total_api_duration_ms,
            "total_tool_duration_ms": self.state.total_tool_duration_ms,
            "total_lines_added": self.state.total_lines_added,
            "total_lines_removed": self.state.total_lines_removed,
            "token_usage": {
                "input_tokens": self.state.token_usage.input_tokens,
                "output_tokens": self.state.token_usage.output_tokens,
                "cache_read_input_tokens": self.state.token_usage.cache_read_input_tokens,
                "cache_creation_input_tokens": self.state.token_usage.cache_creation_input_tokens,
                "web_search_requests": self.state.token_usage.web_search_requests,
            },
            "model_usage": {
                name: {
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "cache_read_input_tokens": u.cache_read_input_tokens,
                    "cache_creation_input_tokens": u.cache_creation_input_tokens,
                    "web_search_requests": u.web_search_requests,
                    "cost_usd": u.cost_usd,
                }
                for name, u in self.state.model_usage.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict, model_name: str = "unknown") -> "CostTracker":
        """Restore state from a plain dict."""
        tracker = cls(model_name=model_name)
        tracker.state.total_cost_usd = data.get("total_cost_usd", 0.0)
        tracker.state.total_api_duration_ms = data.get("total_api_duration_ms", 0.0)
        tracker.state.total_tool_duration_ms = data.get("total_tool_duration_ms", 0.0)
        tracker.state.total_lines_added = data.get("total_lines_added", 0)
        tracker.state.total_lines_removed = data.get("total_lines_removed", 0)
        tu = data.get("token_usage", {})
        tracker.state.token_usage = TokenUsage(
            input_tokens=tu.get("input_tokens", 0),
            output_tokens=tu.get("output_tokens", 0),
            cache_read_input_tokens=tu.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=tu.get("cache_creation_input_tokens", 0),
            web_search_requests=tu.get("web_search_requests", 0),
        )
        for name, u in data.get("model_usage", {}).items():
            tracker.state.model_usage[name] = ModelUsage(
                input_tokens=u.get("input_tokens", 0),
                output_tokens=u.get("output_tokens", 0),
                cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
                cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
                web_search_requests=u.get("web_search_requests", 0),
                cost_usd=u.get("cost_usd", 0.0),
            )
        return tracker


def save_cost_state(workspace_root: str, session_id: str, tracker: CostTracker) -> None:
    """Save cost state to .codelet/config.json for session resume."""
    config_path = Path(workspace_root) / ".codelet" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = tracker.to_dict()
    # Add reference-agent-compatible aliases
    data["last_cost"] = data["total_cost_usd"]
    data["last_api_duration"] = data["total_api_duration_ms"]
    data["last_tool_duration"] = data["total_tool_duration_ms"]
    data["last_lines_added"] = data["total_lines_added"]
    data["last_lines_removed"] = data["total_lines_removed"]
    data["last_model_usage"] = data["model_usage"]
    data["last_session_id"] = session_id
    config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_cost_state(workspace_root: str, session_id: str) -> Optional[CostTracker]:
    """Load cost state from .codelet/config.json if session IDs match."""
    config_path = Path(workspace_root) / ".codelet" / "config.json"
    if not config_path.is_file():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("last_session_id") != session_id:
        return None
    return CostTracker.from_dict(data)
