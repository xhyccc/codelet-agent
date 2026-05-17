"""Connector base + registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class Connector(Protocol):
    name: str
    description: str
    schema: dict[str, str]

    def invoke(self, args: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ConnectorRegistry:
    """Holds named connectors and surfaces them as a tool list."""

    _connectors: dict[str, Connector] = field(default_factory=dict)

    def register(self, connector: Connector) -> None:
        if connector.name in self._connectors:
            raise ValueError(f"connector already registered: {connector.name}")
        self._connectors[connector.name] = connector

    def get(self, name: str) -> Connector:
        return self._connectors[name]

    def names(self) -> list[str]:
        return sorted(self._connectors.keys())

    def as_tool_list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": c.name,
                "description": c.description,
                "parameters": c.schema,
            }
            for c in self._connectors.values()
        ]

    def invoke(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return self._connectors[name].invoke(args)
