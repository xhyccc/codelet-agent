"""Microsoft Graph semantic-route stub.

We don't call the live Graph API in v1. Instead, we resolve the user's
intent into a ``(method, endpoint, params)`` triple that an outer adapter
(or the real :mod:`msgraph` SDK) would dispatch. This isolates the
intent-routing logic so it's unit-testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_ROUTES = [
    # (regex, method, endpoint template, hint params)
    (re.compile(r"\b(unread\s+)?emails?\b", re.I), "GET", "/me/messages", {"$top": 25, "$orderby": "receivedDateTime desc"}),
    (re.compile(r"\b(calendar|meeting|event)s?\b", re.I), "GET", "/me/events", {"$top": 25}),
    (re.compile(r"\b(file|document|onedrive)s?\b", re.I), "GET", "/me/drive/root/children", {}),
    (re.compile(r"\b(team|channel|chat)s?\b", re.I), "GET", "/me/joinedTeams", {}),
    (re.compile(r"\b(contact|people)s?\b", re.I), "GET", "/me/contacts", {}),
]


@dataclass
class MicrosoftGraphConnector:
    name: str = "ms_graph"
    description: str = "Route a natural-language intent to a Microsoft Graph endpoint."
    schema: dict[str, str] = field(
        default_factory=lambda: {"query": "string"}
    )

    def route(self, query: str) -> dict[str, Any]:
        for pattern, method, endpoint, params in _ROUTES:
            if pattern.search(query):
                return {"method": method, "endpoint": endpoint, "params": dict(params)}
        return {"method": "GET", "endpoint": "/me", "params": {}}

    def invoke(self, args: dict[str, Any]) -> dict[str, Any]:
        q = args.get("query")
        if not q:
            return {"ok": False, "error": "missing 'query'"}
        return {"ok": True, "route": self.route(q)}
