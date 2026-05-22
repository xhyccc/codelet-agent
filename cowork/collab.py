"""Collaborative editing primitives for cowork (v1).

This is the stdlib-only stand-in for the planned Yjs / y-py CRDT stack:

* ``LWWMap`` — last-writer-wins map keyed by string, stamped with a Lamport
  clock and a replica id. Concurrent updates converge deterministically.
* ``LWWText`` — append-only / set-content text register on top of LWWMap.
* ``FileLockManager`` — advisory locks with TTL, the v1 substitute for
  Redis Redlock. Locks are stored in process memory but exposed with the
  same acquire/release/refresh API so a Redis adapter can swap in later.

These primitives are intended for collaborative artifact editing inside a
workspace; they intentionally avoid touching the filesystem so they remain
deterministic and easy to test.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Lamport clock + LWW map
# ---------------------------------------------------------------------------

@dataclass(order=True)
class _Stamp:
    """Strict total order for LWW values: (clock, replica_id)."""
    clock: int
    replica_id: str

    def merge(self, other: "_Stamp") -> "_Stamp":
        return other if other > self else self


@dataclass
class LWWEntry:
    value: Any
    stamp: _Stamp


class LWWMap:
    """Thread-safe last-writer-wins map, deterministic on conflict.

    On conflicting writes with the same logical clock, the entry from the
    replica with the lexicographically larger id wins. Two replicas that see
    the same set of updates always converge to byte-identical state.
    """

    def __init__(self, replica_id: str):
        if not replica_id:
            raise ValueError("replica_id required")
        self._replica_id = replica_id
        self._clock = 0
        self._entries: dict[str, LWWEntry] = {}
        self._lock = threading.RLock()

    # ---- core ops ------------------------------------------------------
    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def set(self, key: str, value: Any) -> _Stamp:
        with self._lock:
            stamp = _Stamp(clock=self._tick(), replica_id=self._replica_id)
            cur = self._entries.get(key)
            if cur is None or stamp > cur.stamp:
                self._entries[key] = LWWEntry(value=value, stamp=stamp)
            return stamp

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            e = self._entries.get(key)
            return e.value if e is not None else default

    def delete(self, key: str) -> _Stamp:
        # Represent delete as a tombstone (value=None) with bumped clock.
        with self._lock:
            stamp = _Stamp(clock=self._tick(), replica_id=self._replica_id)
            self._entries[key] = LWWEntry(value=None, stamp=stamp)
            return stamp

    def items(self) -> list[tuple[str, Any]]:
        with self._lock:
            return [(k, e.value) for k, e in self._entries.items() if e.value is not None]

    def snapshot(self) -> dict[str, LWWEntry]:
        with self._lock:
            return {k: LWWEntry(value=e.value, stamp=_Stamp(e.stamp.clock, e.stamp.replica_id))
                    for k, e in self._entries.items()}

    # ---- merge ---------------------------------------------------------
    def merge(self, other_snapshot: dict[str, LWWEntry]) -> int:
        """Merge another replica's snapshot. Returns number of keys updated."""
        updated = 0
        with self._lock:
            for k, oe in other_snapshot.items():
                cur = self._entries.get(k)
                if cur is None or oe.stamp > cur.stamp:
                    self._entries[k] = LWWEntry(value=oe.value, stamp=oe.stamp)
                    # Advance local clock past remote to preserve happens-after.
                    if oe.stamp.clock > self._clock:
                        self._clock = oe.stamp.clock
                    updated += 1
        return updated


# ---------------------------------------------------------------------------
# LWW text register
# ---------------------------------------------------------------------------

class LWWText:
    """Single-key text register. Replaces the document body atomically."""

    KEY = "__text__"

    def __init__(self, replica_id: str, initial: str = ""):
        self._map = LWWMap(replica_id)
        if initial:
            self._map.set(self.KEY, initial)

    def set(self, text: str) -> None:
        self._map.set(self.KEY, text)

    def get(self) -> str:
        return self._map.get(self.KEY, default="") or ""

    def snapshot(self) -> dict[str, LWWEntry]:
        return self._map.snapshot()

    def merge(self, other_snapshot: dict[str, LWWEntry]) -> None:
        self._map.merge(other_snapshot)


# ---------------------------------------------------------------------------
# File lock manager (advisory, with TTL)
# ---------------------------------------------------------------------------

@dataclass
class FileLock:
    workspace_id: str
    path: str
    holder: str  # agent id or user id
    acquired_at: float
    expires_at: float
    token: str  # opaque release token


class LockError(Exception):
    pass


class FileLockManager:
    """In-process advisory lock manager keyed by (workspace_id, path).

    Mirrors the plan's Redis Redlock semantics:
      * acquire(workspace, path, holder, ttl) -> FileLock or raises LockError
      * release(lock) -> bool (true if released, false if already expired/taken over)
      * refresh(lock, ttl) -> FileLock (extends TTL); raises if no longer held
    """

    def __init__(self, default_ttl: float = 30.0):
        self.default_ttl = default_ttl
        self._locks: dict[tuple[str, str], FileLock] = {}
        self._lock = threading.RLock()
        self._counter = 0

    def _now(self) -> float:
        return time.monotonic()

    def _new_token(self) -> str:
        self._counter += 1
        return f"tok_{self._counter}_{int(self._now() * 1000)}"

    def acquire(
        self,
        workspace_id: str,
        path: str,
        holder: str,
        ttl: Optional[float] = None,
    ) -> FileLock:
        ttl = ttl if ttl is not None else self.default_ttl
        with self._lock:
            key = (workspace_id, path)
            cur = self._locks.get(key)
            now = self._now()
            if cur is not None and cur.expires_at > now and cur.holder != holder:
                raise LockError(
                    f"path {path!r} in workspace {workspace_id!r} is held by {cur.holder!r}"
                )
            lock = FileLock(
                workspace_id=workspace_id,
                path=path,
                holder=holder,
                acquired_at=now,
                expires_at=now + ttl,
                token=self._new_token(),
            )
            self._locks[key] = lock
            return lock

    def release(self, lock: FileLock) -> bool:
        with self._lock:
            key = (lock.workspace_id, lock.path)
            cur = self._locks.get(key)
            if cur is None:
                return False
            if cur.token != lock.token:
                return False
            del self._locks[key]
            return True

    def refresh(self, lock: FileLock, ttl: Optional[float] = None) -> FileLock:
        ttl = ttl if ttl is not None else self.default_ttl
        with self._lock:
            key = (lock.workspace_id, lock.path)
            cur = self._locks.get(key)
            if cur is None or cur.token != lock.token:
                raise LockError("lock no longer held")
            cur.expires_at = self._now() + ttl
            return cur

    def is_held(self, workspace_id: str, path: str) -> bool:
        with self._lock:
            cur = self._locks.get((workspace_id, path))
            if cur is None:
                return False
            return cur.expires_at > self._now()

    def list_for_workspace(self, workspace_id: str) -> list[FileLock]:
        with self._lock:
            now = self._now()
            return [l for (ws, _), l in self._locks.items() if ws == workspace_id and l.expires_at > now]
