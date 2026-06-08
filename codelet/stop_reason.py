"""Structured termination reasons for :meth:`MiniAgent.ask`.

The agent's main loop can stop for many different reasons (model returned a
final answer, the step limit was hit, the compaction cascade hard-halted,
etc.).  Historically ``ask()`` only returned a ``str``, which conflated
"clean success" with "graceful failure" and forced callers to scan the text
for known phrases.

This module exposes :class:`StopReason` (an ``Enum``) and the
:class:`AskResult` dataclass.  ``ask()`` still returns a plain ``str`` for
backward compatibility, but it now also stores the structured result on
``MiniAgent.last_ask_result`` and the reason on
``MiniAgent.last_stop_reason``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List


class StopReason(str, Enum):
    """Why the agent's tool loop terminated."""

    FINAL = "final"
    TOOL_ERROR_UNRECOVERABLE = "tool_error_unrecoverable"
    STEP_LIMIT = "step_limit"
    ATTEMPT_LIMIT = "attempt_limit"
    HARD_HALT_RECOVERED = "hard_halt_recovered"
    USER_INTERRUPT = "user_interrupt"
    REPEATED_ERROR_GIVEUP = "repeated_error_giveup"
    BUDGET_EXCEEDED = "budget_exceeded"
    NO_PROGRESS_GIVEUP = "no_progress_giveup"


@dataclass
class AskResult:
    """Structured outcome of a single :meth:`MiniAgent.ask` call."""

    final: str
    reason: StopReason
    tool_steps: int = 0
    attempts: int = 0
    compaction_stages: List[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.final
