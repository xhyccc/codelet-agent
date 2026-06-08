"""Granular permission rules for codelet tools.

Mirrors the reference agent's ToolPermissionContext with:
- always_allow_rules: auto-approve matching tools
- always_deny_rules: auto-reject matching tools
- always_ask_rules: always prompt for matching tools
- permission_denials: track denied requests for reporting
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set


class PermissionRules:
    """A set of permission rules keyed by tool name.

    Each entry is ``tool_name -> list of glob patterns`` (``*`` means all).
    """

    def __init__(self, rules: Optional[Dict[str, List[str]]] = None):
        self.rules: Dict[str, List[str]] = dict(rules or {})

    def matches(self, tool_name: str, command: Optional[str] = None) -> bool:
        """Return True if ``tool_name`` matches any rule in this set."""
        patterns = self.rules.get(tool_name, [])
        if "*" in patterns:
            return True
        if command and patterns:
            # Simple substring match for command-specific rules
            return any(p in command for p in patterns if p != "*")
        return bool(patterns)


class PermissionContext:
    """Full permission context for an agent session."""

    def __init__(
        self,
        mode: str = "default",
        always_allow: Optional[Dict[str, List[str]]] = None,
        always_deny: Optional[Dict[str, List[str]]] = None,
        always_ask: Optional[Dict[str, List[str]]] = None,
    ):
        self.mode = mode
        self.always_allow = PermissionRules(always_allow)
        self.always_deny = PermissionRules(always_deny)
        self.always_ask = PermissionRules(always_ask)
        self.denials: List[dict] = []

    def check(
        self,
        tool_name: str,
        args: dict,
        approval_policy: str,
        read_only: bool,
    ) -> str:
        """Check permission for a tool call.

        Returns one of: ``"allow"``, ``"deny"``, ``"ask"``.
        """
        if read_only:
            return "deny"

        command = str(args.get("command", "")) if tool_name == "run_shell" else ""

        if self.always_deny.matches(tool_name, command):
            return "deny"
        if self.always_allow.matches(tool_name, command):
            return "allow"
        if self.always_ask.matches(tool_name, command):
            return "ask"

        # Fall back to global approval policy
        if approval_policy == "auto":
            return "allow"
        if approval_policy == "never":
            return "deny"
        return "ask"

    def record_denial(self, tool_name: str, args: dict) -> None:
        """Record a permission denial for SDK reporting."""
        self.denials.append({
            "tool_name": tool_name,
            "tool_input": dict(args),
        })

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "always_allow_rules": dict(self.always_allow.rules),
            "always_deny_rules": dict(self.always_deny.rules),
            "always_ask_rules": dict(self.always_ask.rules),
        }


def get_empty_permission_context() -> PermissionContext:
    """Return a default permission context with no rules."""
    return PermissionContext()
