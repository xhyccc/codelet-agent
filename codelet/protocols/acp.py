"""ACP (Agent Communication Protocol) session wrapper.

Uses the official ``acp-sdk`` package for data types.  The
:class:`ACPSessionStub` class provides a convenient in-process session
object built on top of the official :mod:`acp_sdk.models` types.

Official ACP model types are also re-exported from this module so callers
can import them directly::

    from codelet.protocols.acp import Message, MessagePart, Run, Session

If you need a full ACP server, install ``acp-sdk`` and use
``acp_sdk.server.Server`` directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Official ACP SDK types -------------------------------------------------------
from acp_sdk.models import Message  # noqa: F401
from acp_sdk.models import MessagePart  # noqa: F401
from acp_sdk.models import Run  # noqa: F401
from acp_sdk.models import RunStatus  # noqa: F401
from acp_sdk.models import Session  # noqa: F401


class ACPSessionStub:
    """In-process ACP session backed by official ``acp_sdk.models`` types.

    Stores conversation turns as :class:`acp_sdk.models.Message` objects
    and exposes a simple ``append_*`` API for adding turns.  Call
    :meth:`to_dict` to get a plain-dict representation suitable for JSON
    serialization or bridging into a full ACP implementation.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._messages: List[Message] = []
        self.pending_tool_calls: List[Dict[str, Any]] = []
        self.metadata: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    #  Convenience builders                                                #
    # ------------------------------------------------------------------ #

    def append_user_turn(self, text: str) -> None:
        """Append a user-role turn with a plain-text part."""
        self._messages.append(
            Message(role="user", parts=[MessagePart(content=text, content_type="text/plain")])
        )

    def append_agent_turn(
        self,
        text: str,
        *,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Append an agent-role turn, optionally with tool calls."""
        msg = Message(role="agent", parts=[MessagePart(content=text, content_type="text/plain")])
        self._messages.append(msg)
        if tool_calls:
            self.pending_tool_calls.extend(tool_calls)

    # ------------------------------------------------------------------ #
    #  Serialisation                                                       #
    # ------------------------------------------------------------------ #

    @property
    def turns(self) -> List[Dict[str, Any]]:
        """Return turns as plain dicts (role + content string)."""
        result: List[Dict[str, Any]] = []
        for msg in self._messages:
            # Concatenate all text parts into a single content string.
            content = "".join(
                (p.content or "") for p in msg.parts
                # Include plain-text parts (content_type may be None, "text/plain",
                # or "" when the sender omits the MIME type).
                if p.content_type in (None, "text/plain", "")
            )
            turn: Dict[str, Any] = {"role": msg.role, "content": content}
            result.append(turn)
        # Attach pending tool calls onto the last agent turn if present.
        if self.pending_tool_calls and result:
            # Find last agent turn and add tool_calls to it.
            for turn in reversed(result):
                if turn.get("role") == "agent":
                    turn["tool_calls"] = list(self.pending_tool_calls)
                    break
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": self.turns,
            "pending_tool_calls": list(self.pending_tool_calls),
            "metadata": dict(self.metadata),
        }
