"""F13 – Natural-language task scheduler (Phase 2 background-task UI).

Maps NL schedule phrases to cron expressions and keeps an in-memory
registry of scheduled tasks with enable/disable and run-tracking.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Natural-language → cron expression
# ---------------------------------------------------------------------------

def nl_to_cron(text: str) -> str:
    """Best-effort conversion of a plain-English schedule to a cron expression.

    Examples
    --------
    "every 15 minutes"  → "*/15 * * * *"
    "every 2 hours"     → "0 */2 * * *"
    "every day at 9am"  → "0 9 * * *"
    "every morning"     → "0 9 * * *"
    "every weekday"     → "0 9 * * 1-5"
    "weekly"            → "0 9 * * 1"
    """
    t = text.strip()

    # every N minutes
    m = re.search(r"every\s+(\d+)\s+minutes?", t, re.I)
    if m:
        return f"*/{m.group(1)} * * * *"

    # every N hours
    m = re.search(r"every\s+(\d+)\s+hours?", t, re.I)
    if m:
        return f"0 */{m.group(1)} * * *"

    # every (day|night|morning|evening) at HH[:MM] [am|pm]
    m = re.search(
        r"every\s+(?:day|night|morning|evening)?\s*at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        t, re.I,
    )
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        period = (m.group(3) or "").lower()
        if period == "pm" and hour != 12:
            hour += 12
        elif period == "am" and hour == 12:
            hour = 0
        return f"{minute} {hour} * * *"

    _simple: list[tuple[re.Pattern, str]] = [
        (re.compile(r"every\s+minute",   re.I), "* * * * *"),
        (re.compile(r"every\s+hour",     re.I), "0 * * * *"),
        (re.compile(r"every\s+morning",  re.I), "0 9 * * *"),
        (re.compile(r"every\s+evening",  re.I), "0 18 * * *"),
        (re.compile(r"every\s+night",    re.I), "0 22 * * *"),
        (re.compile(r"every\s+weekday",  re.I), "0 9 * * 1-5"),
        (re.compile(r"every\s+monday",   re.I), "0 9 * * 1"),
        (re.compile(r"every\s+friday",   re.I), "0 9 * * 5"),
        (re.compile(r"every\s+weekend",  re.I), "0 10 * * 0,6"),
        (re.compile(r"\bdaily\b",        re.I), "0 9 * * *"),
        (re.compile(r"\bweekly\b",       re.I), "0 9 * * 1"),
        (re.compile(r"\bmonthly\b",      re.I), "0 9 1 * *"),
    ]
    for pat, cron in _simple:
        if pat.search(t):
            return cron

    # fallback: daily at 9 am
    return "0 9 * * *"


# ---------------------------------------------------------------------------
# Scheduled task dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScheduledTask:
    name: str
    prompt: str
    cron_expr: str
    workspace_id: str = ""
    enabled: bool = True
    last_run: Optional[float] = None
    next_run: Optional[float] = None
    last_status: str = "pending"   # pending | running | done | failed
    id: str = field(default_factory=lambda: _new_id("sched"))
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "cron_expr": self.cron_expr,
            "workspace_id": self.workspace_id,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "last_status": self.last_status,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Scheduler registry (in-memory, thread-safe)
# ---------------------------------------------------------------------------

class TaskScheduler:
    """In-memory registry of scheduled tasks.

    Does not actually execute tasks; a host process may poll ``list()``
    and invoke the relevant runner when ``next_run`` is due.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: dict[str, ScheduledTask] = {}

    def add(self, task: ScheduledTask) -> ScheduledTask:
        with self._lock:
            self._tasks[task.id] = task
        return task

    def remove(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

    def get(self, task_id: str) -> Optional[ScheduledTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[ScheduledTask]:
        with self._lock:
            return list(self._tasks.values())

    def enable(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.enabled = True
                return True
        return False

    def disable(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.enabled = False
                return True
        return False

    def mark_run(self, task_id: str, status: str = "done") -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t:
                t.last_run = _now()
                t.last_status = status

    def toggle(self, task_id: str) -> Optional[bool]:
        """Toggle enabled state. Returns new state or None if not found."""
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return None
            t.enabled = not t.enabled
            return t.enabled
