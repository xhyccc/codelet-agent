"""WeChat Work (WeCom) connector.

* Cached ``access_token`` with TTL.
* ``send_message`` stub that records intent without network.
* Callback signature verifier following the documented SHA-1 scheme
  (sorted tokens -> sha1 digest).
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class WeComConnector:
    corp_id: str = "stub-corp"
    corp_secret: str = "stub-secret"
    token: str = "demo-token"  # callback verification token
    name: str = "wecom"
    description: str = "Send WeCom messages and verify callback signatures."
    schema: dict[str, str] = field(default_factory=lambda: {
        "action": "string",
        "to_user": "string",
        "content": "string",
    })
    _access_token: Optional[str] = None
    _access_token_expires_at: float = 0.0
    _fetch_token: Callable[[], tuple[str, float]] = field(default=None)  # type: ignore
    _sent: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        if self._fetch_token is None:
            def _stub() -> tuple[str, float]:
                return (f"wecom-{int(time.time())}", 7200.0)
            self._fetch_token = _stub

    def get_access_token(self, *, now: Optional[float] = None) -> str:
        t = time.time() if now is None else now
        if self._access_token is None or self._access_token_expires_at - t < 60:
            tok, ttl = self._fetch_token()
            self._access_token = tok
            self._access_token_expires_at = t + ttl
        return self._access_token

    def send_message(self, *, to_user: str, content: str) -> dict[str, Any]:
        token = self.get_access_token()
        payload = {"to_user": to_user, "content": content, "auth": token}
        self._sent.append(payload)
        return {"ok": True, "message_id": f"msg_{len(self._sent)}", "echo": payload}

    # --- callback verification ----------------------------------------
    def verify_signature(self, *, signature: str, timestamp: str, nonce: str, echostr: str = "") -> bool:
        items = sorted([self.token, timestamp, nonce, echostr])
        digest = hashlib.sha1("".join(items).encode("utf-8")).hexdigest()
        return digest == signature

    def make_signature(self, *, timestamp: str, nonce: str, echostr: str = "") -> str:
        items = sorted([self.token, timestamp, nonce, echostr])
        return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()

    def invoke(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action", "send_message")
        if action == "send_message":
            try:
                return self.send_message(to_user=args["to_user"], content=args["content"])
            except KeyError as e:
                return {"ok": False, "error": f"missing field: {e.args[0]}"}
        if action == "get_access_token":
            return {"ok": True, "token": self.get_access_token()}
        return {"ok": False, "error": f"unknown action {action!r}"}
