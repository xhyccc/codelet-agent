"""Artifact engine: parse ``<artifact>`` blocks and render safe HTML.

This is the v1 stand-in for the planned Sandpack iframe renderer. We:

1. Stream-parse ``<artifact type="html|react|markdown|code" title="...">...</artifact>``
   blocks from arbitrary text (typically codelet stdout).
2. Sanitize HTML artifacts with a permissive but safe regex pass
   (drops ``<script>``, on-handlers, ``javascript:`` URIs, ``<iframe>``,
   ``<object>``, ``<embed>``, ``<form>``).
3. Persist the sanitized body to disk under the workspace's artifact
   directory and return an :class:`ArtifactRecord` describing it.

The sanitizer is *not* a full HTML parser; it is intentionally
conservative — anything suspicious is escaped or removed.
"""
from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


_ARTIFACT_RE = re.compile(
    r"<artifact(?P<attrs>[^>]*)>(?P<body>.*?)</artifact>", re.S | re.I
)
_ATTR_RE = re.compile(r"(\w+)=\"([^\"]*)\"")

ALLOWED_KINDS = ("html", "react", "markdown", "code", "json", "svg")

# --- Sanitizer patterns ---------------------------------------------------
# Tags we strip entirely along with their content.
_DANGEROUS_TAG_RE = re.compile(
    r"<\s*(script|iframe|object|embed|form|meta|link|style)\b[^>]*>.*?</\s*\1\s*>",
    re.S | re.I,
)
# Self-closing variants of dangerous tags.
_DANGEROUS_SELF_RE = re.compile(
    r"<\s*(script|iframe|object|embed|form|meta|link|style)\b[^>]*/?>",
    re.I,
)
# Inline event handlers: onclick=, onload=, etc.
_ON_HANDLER_RE = re.compile(r"\s+on[a-z]+\s*=\s*\"[^\"]*\"", re.I)
_ON_HANDLER_SQ_RE = re.compile(r"\s+on[a-z]+\s*=\s*'[^']*'", re.I)
_ON_HANDLER_BARE_RE = re.compile(r"\s+on[a-z]+\s*=\s*[^\s>]+", re.I)
# javascript: / data: URIs in href/src
_BAD_URI_RE = re.compile(
    r"(?P<attr>(?:href|src|action|formaction))\s*=\s*\"\s*(?:javascript|vbscript|data):[^\"]*\"",
    re.I,
)


@dataclass
class ArtifactRecord:
    kind: str
    title: str
    path: str  # workspace-relative path to the persisted artifact
    sanitized: bool
    body: str  # final content actually written
    id: str = field(default_factory=lambda: f"art_{uuid.uuid4().hex[:12]}")
    attrs: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

def sanitize_html(body: str) -> str:
    """Apply a conservative pass to remove obvious XSS vectors.

    Returns the cleaned HTML. The pass is idempotent.
    """
    out = body
    # Remove dangerous tag+content first, then any leftover opening/closing tags.
    out = _DANGEROUS_TAG_RE.sub("", out)
    out = _DANGEROUS_SELF_RE.sub("", out)
    out = _ON_HANDLER_RE.sub("", out)
    out = _ON_HANDLER_SQ_RE.sub("", out)
    out = _ON_HANDLER_BARE_RE.sub("", out)
    out = _BAD_URI_RE.sub(lambda m: f'{m.group("attr")}="#blocked"', out)
    return out


def wrap_iframe_document(body: str, *, title: str = "Artifact") -> str:
    """Wrap a sanitized HTML body in a sandbox-ready document shell."""
    safe_title = html.escape(title or "Artifact")
    return (
        "<!doctype html>\n"
        f"<html><head><meta charset=\"utf-8\"><title>{safe_title}</title></head>"
        f"<body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_artifacts(raw: str) -> list[tuple[dict[str, str], str]]:
    """Extract ``(attrs_dict, body)`` tuples from raw text."""
    out: list[tuple[dict[str, str], str]] = []
    for m in _ARTIFACT_RE.finditer(raw):
        attrs = dict(_ATTR_RE.findall(m.group("attrs") or ""))
        out.append((attrs, m.group("body")))
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ArtifactEngine:
    """Parses, sanitizes, and persists artifacts under a workspace dir."""

    def __init__(self, workspace_root: Path, *, subdir: str = ".cowork/artifacts"):
        self.workspace_root = Path(workspace_root)
        self.subdir = subdir
        self.artifact_root = self.workspace_root / subdir
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    # ---- write a single artifact ---------------------------------------
    def store(self, attrs: dict[str, str], body: str) -> ArtifactRecord:
        kind = (attrs.get("type") or attrs.get("kind") or "code").lower()
        if kind not in ALLOWED_KINDS:
            kind = "code"
        title = attrs.get("title") or f"untitled-{kind}"
        ext = self._extension_for(kind)
        sanitized = False
        final_body = body
        if kind == "html":
            final_body = sanitize_html(body)
            final_body = wrap_iframe_document(final_body, title=title)
            sanitized = True
        elif kind == "svg":
            final_body = sanitize_html(body)
            sanitized = True
        # Compute a slugged filename.
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", title).strip("-").lower() or "artifact"
        fname = f"{slug}-{uuid.uuid4().hex[:6]}{ext}"
        dest = self.artifact_root / fname
        dest.write_text(final_body, encoding="utf-8")
        rel_path = str(dest.relative_to(self.workspace_root))
        return ArtifactRecord(
            kind=kind,
            title=title,
            path=rel_path,
            sanitized=sanitized,
            body=final_body,
            attrs=attrs,
        )

    # ---- process a raw stream -----------------------------------------
    def ingest(self, raw: str) -> list[ArtifactRecord]:
        records = []
        for attrs, body in parse_artifacts(raw):
            records.append(self.store(attrs, body))
        return records

    @staticmethod
    def _extension_for(kind: str) -> str:
        return {
            "html": ".html",
            "react": ".jsx",
            "markdown": ".md",
            "code": ".txt",
            "json": ".json",
            "svg": ".svg",
        }.get(kind, ".txt")
