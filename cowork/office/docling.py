"""Docling-style document-to-markdown extractor.

We do not bundle the real Docling model in v1. Instead, the connector
holds a swappable ``_convert(path)`` callable that returns markdown.
Tests inject a fixture implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


def _default_convert(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(str(path))
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"# {path.name}\n\n*(binary content, {path.stat().st_size} bytes)*"


@dataclass
class DoclingConnector:
    name: str = "docling"
    description: str = "Extract markdown from documents (PDF/DOCX/HTML)."
    schema: dict[str, str] = field(default_factory=lambda: {"path": "string"})
    _convert: Callable[[Path], str] = field(default=_default_convert)

    def extract(self, path: str) -> dict[str, Any]:
        p = Path(path)
        try:
            md = self._convert(p)
            return {"ok": True, "path": str(p), "markdown": md, "length": len(md)}
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def invoke(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path")
        if not path:
            return {"ok": False, "error": "missing 'path'"}
        return self.extract(path)
