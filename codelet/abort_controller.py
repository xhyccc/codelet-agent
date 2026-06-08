"""Abort controller for codelet.

Mirrors the reference agent's AbortController:
- Signal-based cancellation
- abort() method to trigger cancellation
"""

from __future__ import annotations

import threading


class AbortController:
    """Simple abort controller with a signal flag."""

    def __init__(self):
        self._aborted = False
        self._lock = threading.Lock()

    def abort(self) -> None:
        """Signal abortion."""
        with self._lock:
            self._aborted = True

    @property
    def aborted(self) -> bool:
        with self._lock:
            return self._aborted

    def check(self) -> None:
        """Raise if aborted."""
        if self.aborted:
            raise RuntimeError("Aborted")
