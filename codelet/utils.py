"""Small shared utilities: clipping, formatting, timestamps, constants."""

import re
from datetime import datetime, timezone


# ANSI escape-sequence stripper (CSI + OSC + SGR). Used to scrub tool output
# before it lands in the transcript so the model is not distracted by colors
# and cursor controls.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


# Public constants reused across the package.
DOC_NAMES = ("AGENTS.md", "README.md", "pyproject.toml", "package.json")
HELP_TEXT = "/help, /memory, /session, /reset, /exit"
WELCOME_ART = (
    "/\\     /\\\\",
    "{  `---'  }",
    "{  O   O  }",
    "~~>  V  <~~",
    "\\\\  \\|/  /",
    "`-----'__",
)
HELP_DETAILS = "\n".join(
    [
        "Commands:",
        "/help    Show this help message.",
        "/memory  Show the agent's distilled working memory.",
        "/session Show the path to the saved session file.",
        "/reset   Clear the current session history and memory.",
        "/exit    Exit the agent.",
    ]
)

# Default budgets. Effective values come from the YAML harness config, but these
# preserve sensible behavior when configs are missing.
MAX_TOOL_OUTPUT = 4000
MAX_HISTORY = 12000
IGNORED_PATH_NAMES = {".git", ".mini-coding-agent", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "venv"}
ALL_TOOL_OPS = frozenset({"read", "write", "bash", "python", "net"})


def now():
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def clip(text, limit=MAX_TOOL_OUTPUT):
    """Truncate a string to `limit` characters, marking the truncation."""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def middle(text, limit):
    """Shorten a string by ellipsizing the middle to fit `limit` characters."""
    text = str(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    left = (limit - 3) // 2
    right = limit - 3 - left
    return text[:left] + "..." + text[-right:]


def strip_ansi(text):
    """Remove ANSI color codes and OSC sequences from ``text``."""
    return _ANSI_RE.sub("", str(text))


def clip_head_tail(text, limit, head_ratio=0.6):
    """Keep the most informative head and tail of a long string.

    Long tool outputs (compilation logs, large traces) usually carry the
    most relevant signal in the very first lines (what failed) and the very
    last lines (the final error). ``clip_head_tail`` keeps both, ellipsizing
    the middle, so we sacrifice the least useful section.
    """
    text = str(text)
    if len(text) <= limit:
        return text
    if limit <= 32:
        return text[:limit]
    head_chars = max(64, int((limit - 32) * head_ratio))
    tail_chars = max(32, (limit - 32) - head_chars)
    middle_msg = f"\n...[clipped {len(text) - head_chars - tail_chars} chars]...\n"
    return text[:head_chars] + middle_msg + text[-tail_chars:]


def dedupe_lines(text, *, max_repeats=3):
    """Collapse runs of identical consecutive lines.

    ``[same: line]`` markers are inserted whenever a line repeats more than
    ``max_repeats`` times in a row.
    """
    lines = str(text).splitlines()
    out = []
    prev = None
    count = 0
    for line in lines:
        if line == prev:
            count += 1
            if count <= max_repeats:
                out.append(line)
            elif count == max_repeats + 1:
                out.append(f"... [previous line repeats; will be collapsed]")
            continue
        if prev is not None and count > max_repeats:
            out.append(f"[same line repeated {count} times total]")
        prev = line
        count = 1
        out.append(line)
    if prev is not None and count > max_repeats:
        out.append(f"[same line repeated {count} times total]")
    return "\n".join(out)
