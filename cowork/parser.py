"""Streaming parser for codelet subprocess stdout.

Codelet emits final assistant text on stdout. The model's reasoning surface
includes XML-ish blocks: ``<tool name="..."> {json} </tool>`` and
``<final> ... </final>``. This parser extracts both as structured events so
cowork can route them to artifacts, audit, and the event bus without
coupling to codelet's internals.

The parser is *resilient*: malformed blocks are returned as plain text rather
than raising, mirroring codelet's own permissive `parse_xml_tool`.

When codelet runs in ``--machine`` mode it XML-escapes the tag bodies (name
attribute, JSON args, and final text) to prevent special characters from
breaking the surrounding tags.  The parser transparently unescapes these
before further processing so callers always receive decoded values.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Iterator, Optional, Union


# Pre-compiled patterns. Use non-greedy bodies and DOTALL so multi-line bodies
# survive parsing.
_TOOL_RE = re.compile(
    r"<tool(?P<attrs>[^>]*)>(?P<body>.*?)</tool>", re.S | re.I
)
_FINAL_RE = re.compile(r"<final>(?P<body>.*?)</final>", re.S | re.I)
_ATTR_RE = re.compile(r"(\w+)=\"([^\"]*)\"")


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

@dataclass
class TextChunk:
    text: str


@dataclass
class ToolCall:
    name: str
    args: dict
    raw: str


@dataclass
class FinalAnswer:
    text: str


ParseEvent = Union[TextChunk, ToolCall, FinalAnswer]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_tool_body(attrs: str, body: str) -> Optional[ToolCall]:
    # Two schemas: (a) name in attrs + JSON args body; (b) full JSON body with "tool" key.
    attrs_dict = dict(_ATTR_RE.findall(attrs or ""))
    # Unescape the name in case it was XML-escaped by --machine mode.
    raw_name = attrs_dict.get("name")
    name = html.unescape(raw_name) if raw_name is not None else None
    # Unescape the body: --machine mode XML-escapes it to protect the tags.
    body_stripped = html.unescape(body).strip()
    args: dict = {}
    if name:
        if body_stripped:
            try:
                parsed = json.loads(body_stripped)
                if isinstance(parsed, dict):
                    args = parsed
            except json.JSONDecodeError:
                # Treat the body as a raw "input" string.
                args = {"_raw": body_stripped}
        return ToolCall(name=name, args=args, raw=body)
    # No name in attrs: try JSON body
    try:
        parsed = json.loads(body_stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    nm = parsed.get("tool") or parsed.get("name")
    if not nm or not isinstance(nm, str):
        return None
    args = parsed.get("args") or {k: v for k, v in parsed.items() if k not in ("tool", "name")}
    if not isinstance(args, dict):
        args = {}
    return ToolCall(name=nm, args=args, raw=body)


def parse_codelet_output(raw: str) -> list[ParseEvent]:
    """Parse a complete codelet stdout string into ordered events.

    Order is preserved: text between tags becomes TextChunk events; tool and
    final blocks become their own events. Empty text chunks are skipped.
    """
    if not raw:
        return []

    # Build a list of (start, end, event) spans.
    spans: list[tuple[int, int, ParseEvent]] = []

    for m in _TOOL_RE.finditer(raw):
        ev = _parse_tool_body(m.group("attrs"), m.group("body"))
        if ev is not None:
            spans.append((m.start(), m.end(), ev))
    for m in _FINAL_RE.finditer(raw):
        spans.append((m.start(), m.end(), FinalAnswer(text=html.unescape(m.group("body")).strip())))

    spans.sort(key=lambda s: s[0])

    events: list[ParseEvent] = []
    cursor = 0
    for start, end, ev in spans:
        if start > cursor:
            chunk = raw[cursor:start]
            if chunk.strip():
                events.append(TextChunk(text=chunk))
        events.append(ev)
        cursor = end
    if cursor < len(raw):
        tail = raw[cursor:]
        if tail.strip():
            events.append(TextChunk(text=tail))
    return events


def iter_codelet_output(raw: str) -> Iterator[ParseEvent]:
    """Streaming alias returning a generator."""
    yield from parse_codelet_output(raw)


def extract_final(raw: str) -> Optional[str]:
    """Return the last <final>...</final> body, or None if absent."""
    matches = _FINAL_RE.findall(raw)
    return html.unescape(matches[-1]).strip() if matches else None
