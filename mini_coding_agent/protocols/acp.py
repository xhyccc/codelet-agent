"""ACP (Agent Communication Protocol) scaffolding stub.

This module deliberately ships only a *very* lean stub.  ACP is a
larger, still-evolving spec; bundling a half-finished implementation
would create more confusion than value.  Instead we expose:

* :class:`ACPSessionStub` - a typed scratchpad your code can use to
  collect ACP-style messages while you wire up a real implementation.

If you need a full ACP stack today, install one of the existing
reference SDKs and use it directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ACPSessionStub:
    """A pure-data scaffold for ACP-style sessions.

    The fields mirror the headline ACP concepts (session id, turn list,
    pending tool calls).  No transport or negotiation logic is provided;
    you can serialize the dataclass to JSON if you want to bridge into a
    real implementation later.
    """

    session_id: str
    turns: List[Dict[str, Any]] = field(default_factory=list)
    pending_tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def append_user_turn(self, text: str) -> None:
        self.turns.append({"role": "user", "content": text})

    def append_agent_turn(self, text: str, *, tool_calls: Optional[List[Dict[str, Any]]] = None) -> None:
        turn: Dict[str, Any] = {"role": "agent", "content": text}
        if tool_calls:
            turn["tool_calls"] = tool_calls
        self.turns.append(turn)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": list(self.turns),
            "pending_tool_calls": list(self.pending_tool_calls),
            "metadata": dict(self.metadata),
        }
