"""Zoom Server-to-Server OAuth token cache + send-meeting stub.

The ``_fetch_token`` seam is intended to be monkey-patched in tests.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class ZoomConnector:
    account_id: str = "stub-account"
    client_id: str = "stub-client"
    client_secret: str = "stub-secret"
    name: str = "zoom"
    description: str = "Schedule Zoom meetings via Server-to-Server OAuth."
    schema: dict[str, str] = field(default_factory=lambda: {
        "action": "string",
        "topic": "string",
        "start_time": "string",
        "duration_minutes": "int",
    })
    _token: Optional[str] = None
    _token_expires_at: float = 0.0
    _refresh_window: float = 60.0  # refresh if <60s left
    _fetch_token: Callable[[], tuple[str, float]] = field(default=None)  # type: ignore

    def __post_init__(self):
        if self._fetch_token is None:
            # default: deterministic stub
            def _stub() -> tuple[str, float]:
                return (f"tok-{int(time.time())}", 3600.0)
            self._fetch_token = _stub

    def get_token(self, *, now: Optional[float] = None) -> str:
        t = time.time() if now is None else now
        if self._token is None or self._token_expires_at - t < self._refresh_window:
            tok, ttl = self._fetch_token()
            self._token = tok
            self._token_expires_at = t + ttl
        return self._token

    def create_meeting(self, *, topic: str, start_time: str, duration_minutes: int = 30) -> dict[str, Any]:
        token = self.get_token()
        return {
            "ok": True,
            "auth_used": token,
            "meeting": {
                "topic": topic,
                "start_time": start_time,
                "duration": duration_minutes,
                "join_url": f"https://zoom.us/j/stub?token={token[:6]}",
            },
        }

    def invoke(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action", "create_meeting")
        if action == "create_meeting":
            try:
                return self.create_meeting(
                    topic=args["topic"],
                    start_time=args["start_time"],
                    duration_minutes=int(args.get("duration_minutes", 30)),
                )
            except KeyError as e:
                return {"ok": False, "error": f"missing field: {e.args[0]}"}
        if action == "get_token":
            return {"ok": True, "token": self.get_token()}
        return {"ok": False, "error": f"unknown action {action!r}"}
