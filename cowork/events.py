"""In-process pub/sub event bus with per-workspace and per-session channels.

This is the v1 substitute for the planned Redis Pub/Sub + Socket.IO fan-out.
The API mirrors the channel semantics (`workspace:{id}`, `session:{id}`,
`tenant:{id}:budget`) so a later Redis adapter can drop in without touching
callers.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """A typed event flowing through the bus."""
    kind: str  # e.g. "agent.output", "lock.acquired", "artifact.ready", "budget.exceeded"
    channel: str  # e.g. "workspace:ws_xxx"
    payload: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Channel helpers
# ---------------------------------------------------------------------------

def workspace_channel(workspace_id: str) -> str:
    return f"workspace:{workspace_id}"


def session_channel(session_id: str) -> str:
    return f"session:{session_id}"


def tenant_budget_channel(tenant_id: str) -> str:
    return f"tenant:{tenant_id}:budget"


# ---------------------------------------------------------------------------
# Subscription handle
# ---------------------------------------------------------------------------

Handler = Callable[[Event], None]


@dataclass
class Subscription:
    id: str
    channel: str
    handler: Handler
    _bus: "EventBus"

    def cancel(self) -> None:
        self._bus._unsubscribe(self)


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------

class EventBus:
    """Thread-safe in-process pub/sub with bounded per-channel history."""

    def __init__(self, history_per_channel: int = 100):
        self._lock = threading.RLock()
        self._subs: dict[str, list[Subscription]] = defaultdict(list)
        self._history: dict[str, deque[Event]] = defaultdict(
            lambda: deque(maxlen=history_per_channel)
        )
        self._history_max = history_per_channel

    # ---- subscribe / publish -------------------------------------------
    def subscribe(self, channel: str, handler: Handler) -> Subscription:
        sub = Subscription(id=uuid.uuid4().hex[:12], channel=channel, handler=handler, _bus=self)
        with self._lock:
            self._subs[channel].append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            subs = self._subs.get(sub.channel, [])
            self._subs[sub.channel] = [s for s in subs if s.id != sub.id]

    def publish(self, event: Event) -> int:
        """Dispatch an event to every subscriber on its channel.

        Returns the number of handlers invoked. Handler exceptions are
        swallowed (logged via stderr) so one bad subscriber cannot kill
        delivery to the rest.
        """
        with self._lock:
            subs = list(self._subs.get(event.channel, []))
            self._history[event.channel].append(event)
        count = 0
        for sub in subs:
            try:
                sub.handler(event)
                count += 1
            except Exception as e:  # pragma: no cover - defensive
                import sys
                print(f"[eventbus] handler {sub.id} raised: {e!r}", file=sys.stderr)
        return count

    # ---- history / replay ----------------------------------------------
    def history(self, channel: str, *, since: Optional[float] = None) -> list[Event]:
        with self._lock:
            evs = list(self._history.get(channel, ()))
        if since is not None:
            evs = [e for e in evs if e.at >= since]
        return evs

    def clear(self, channel: Optional[str] = None) -> None:
        with self._lock:
            if channel is None:
                self._history.clear()
                self._subs.clear()
            else:
                self._history.pop(channel, None)
                self._subs.pop(channel, None)


# ---------------------------------------------------------------------------
# Convenience: a thread-safe queue subscription (for blocking consumers)
# ---------------------------------------------------------------------------

class QueueSubscription:
    """Drain events into a Queue for thread-safe blocking consumption."""

    def __init__(self, bus: EventBus, channel: str, maxsize: int = 1024):
        import queue
        self.queue: "queue.Queue[Event]" = queue.Queue(maxsize=maxsize)
        self._evict_lock = threading.Lock()
        self._sub = bus.subscribe(channel, self._on)

    def _on(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except Exception:
            # queue full: atomically drop oldest then push
            with self._evict_lock:
                try:
                    self.queue.get_nowait()
                except Exception:
                    pass
                try:
                    self.queue.put_nowait(event)
                except Exception:
                    pass

    def get(self, timeout: Optional[float] = None) -> Event:
        return self.queue.get(timeout=timeout)

    def cancel(self) -> None:
        self._sub.cancel()
