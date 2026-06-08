"""Hierarchical filesystem-backed memory.

Implements the *filesystem persistence* guideline from the Claude Code
architecture: instead of relying on vector databases, the agent reads
markdown protocol files at well-known locations and uses their headers to
pick up to five most-relevant files for inclusion in the prompt.

Layer order (lowest -> highest precedence):

    1. ``/etc/mini-coding-agent/CLAUDE.md``         - system-wide defaults
    2. ``~/.claude/CLAUDE.md``, ``~/.mini-coding-agent/CLAUDE.md``
                                                      - user preferences
    3. ``<repo>/.claude/rules/*.md``,
       ``<repo>/.mini-coding-agent/rules.md``,
       ``<repo>/AGENTS.md``,
       ``<repo>/CLAUDE.md``                          - project records
    4. ``<repo>/CLAUDE.local.md``                    - git-ignored workspace
                                                      notes

Header-based retrieval intentionally avoids semantic vector search; the agent
looks at the first markdown heading (or first non-empty line) of each
candidate file and feeds that summary to an LLM-driven scorer (or to the
caller's selection function). Up to ``max_files`` entries are returned.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Default discovery roots. Empty by default — nothing is auto-loaded from
# global or user home directories unless the config explicitly sets these.
# (Previous defaults pointed at /etc/mini-coding-agent and ~/.claude which
# caused ~/.claude/AGENTS.md, Claude Code's skill registry, to bleed into
# codelet's context.)
DEFAULT_GLOBAL_ROOTS = []
DEFAULT_USER_ROOTS = []
DEFAULT_PROJECT_PATHS = [
    ".claude/rules",          # directory - all *.md inside
    ".mini-coding-agent/rules.md",
    "AGENTS.md",
    "CLAUDE.md",
]
DEFAULT_LOCAL_PATHS = ["CLAUDE.local.md"]

# Cap on how many memory files we expose to the model at once. The protocol
# is intentionally tight: if the user has more files than this, the scorer
# must pick.
DEFAULT_MAX_FILES = 5

# Iter 3: cap the total scan to avoid performance cliffs in large repos.
# Mirrors the TypeScript MAX_MEMORY_FILES = 200 constant.
MAX_SCAN_FILES = 200

# Iter 1/2: closed four-type memory taxonomy (mirrors the TS reference).
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# Iter 5: MEMORY.md index-file constants (mirrors TS ENTRYPOINT_NAME etc.).
ENTRYPOINT_NAME = "MEMORY.md"
MAX_ENTRYPOINT_LINES = 200      # ~125 chars/line × 200 lines at p97
MAX_ENTRYPOINT_BYTES = 25_000   # matches TS MAX_ENTRYPOINT_BYTES

# Iter 1: only scan the first N lines for frontmatter (cheap protection
# against huge files — mirrors TS readFileInRange(0, FRONTMATTER_MAX_LINES)).
FRONTMATTER_MAX_LINES = 30


_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$", re.MULTILINE)

# Iter 1: closing fence regex for YAML-ish frontmatter blocks.
_FRONT_FENCE_RE = re.compile(r"^---[ \t]*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Iter 2/6: MemoryHeader dataclass (mirrors TS MemoryHeader type)
# ---------------------------------------------------------------------------


@dataclass
class MemoryHeader:
    """Rich memory-file descriptor returned by :func:`scan_memory_headers`.

    Mirrors the TypeScript ``MemoryHeader`` type from the reference
    implementation.
    """
    filename: str     # relative to the scanned directory
    file_path: Path   # absolute path
    mtime_ms: float   # modification time in milliseconds
    description: str  # from frontmatter ``description:`` or first heading
    mem_type: str     # one of :data:`MEMORY_TYPES` or ``""`` if unknown


# ---------------------------------------------------------------------------
# Iter 1: Frontmatter parsing
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple:
    """Parse YAML frontmatter from the first ``---...---`` block.

    Only scans the first :data:`FRONTMATTER_MAX_LINES` lines (cheap
    protection against reading huge files — mirrors TS
    ``readFileInRange(0, FRONTMATTER_MAX_LINES)``).

    Returns ``(description, mem_type)``. Both default to ``""`` when the
    frontmatter block is absent, malformed, or when ``type`` is not one of
    :data:`MEMORY_TYPES` (Iter 2: taxonomy validation).
    """
    if not text.startswith("---"):
        return "", ""
    # Only look at the first FRONTMATTER_MAX_LINES lines.
    head_lines = text.split("\n", FRONTMATTER_MAX_LINES)
    head = "\n".join(head_lines[:FRONTMATTER_MAX_LINES])
    # Find the closing fence after the opening "---".
    m = _FRONT_FENCE_RE.search(head, 3)
    if not m:
        return "", ""
    front = head[3:m.start()].strip()
    description = ""
    mem_type = ""
    for line in front.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("description:"):
            description = stripped[len("description:"):].strip().strip('"').strip("'")
        elif lower.startswith("type:"):
            candidate = stripped[len("type:"):].strip().strip('"').strip("'")
            if candidate in MEMORY_TYPES:  # Iter 2: validate against taxonomy
                mem_type = candidate
    return description, mem_type


def _first_header(text):
    """Return the first markdown heading (without ``#``), or the first
    non-empty line if there are no headings."""
    if not text:
        return ""
    match = _HEADING_RE.search(text)
    if match:
        return match.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


# ---------------------------------------------------------------------------
# Iter 4: Memory freshness / staleness tracking
# ---------------------------------------------------------------------------


def memory_age_days(mtime_ms: float) -> int:
    """Days elapsed since *mtime_ms* (milliseconds since epoch).

    Floor-rounded: 0 for today, 1 for yesterday, 2+ for older. Negative
    inputs (future mtime or clock skew) clamp to 0.
    Mirrors the TypeScript ``memoryAgeDays()`` helper.
    """
    return max(0, int((time.time() * 1000.0 - mtime_ms) / 86_400_000))


def memory_freshness_text(mtime_ms: float) -> str:
    """Plain-text staleness caveat for memories > 1 day old.

    Returns ``""`` for fresh (today/yesterday) memories — adding a warning
    there would be noise. For older memories, warns that code-state claims
    and file:line citations may be outdated.
    Mirrors the TypeScript ``memoryFreshnessText()`` helper.
    """
    d = memory_age_days(mtime_ms)
    if d <= 1:
        return ""
    return (
        f"This memory is {d} days old. "
        "Memories are point-in-time observations, not live state — "
        "claims about code behaviour or file:line citations may be outdated. "
        "Verify against current code before asserting as fact."
    )


def _candidate_paths(repo_root, *, global_roots, user_roots, project_paths, local_paths):
    """Expand the configured discovery roots into a flat list of file paths.

    Directories contribute every ``*.md`` file they contain (non-recursive,
    sorted for determinism); explicit file paths contribute themselves.
    Missing entries are silently skipped. Each entry is paired with a layer
    tag (``"global"``, ``"user"``, ``"project"``, ``"local"``) used so the
    caller can apply layer precedence when scoring.
    """
    out = []

    def add(path, layer):
        path = Path(path).expanduser()
        if path.is_dir():
            for entry in sorted(path.glob("*.md")):
                out.append((entry, layer))
        elif path.is_file():
            out.append((path, layer))

    for root in global_roots or []:
        root = Path(root).expanduser()
        if root.is_dir():
            for entry in sorted(root.glob("*.md")):
                out.append((entry, "global"))
        elif root.is_file():
            out.append((root, "global"))

    for root in user_roots or []:
        root = Path(root).expanduser()
        if root.is_dir():
            for entry in sorted(root.glob("*.md")):
                out.append((entry, "user"))
        elif root.is_file():
            out.append((root, "user"))

    repo_root = Path(repo_root) if repo_root else None
    if repo_root:
        for rel in project_paths or []:
            add(repo_root / rel, "project")
        for rel in local_paths or []:
            add(repo_root / rel, "local")

    return out


def discover_memory_files(
    repo_root,
    *,
    global_roots=None,
    user_roots=None,
    project_paths=None,
    local_paths=None,
):
    """Return ``[(path, layer, header)]`` for every memory file found.

    The header is the frontmatter ``description:`` field when present, or the
    first markdown heading (truncated at 160 chars). Files that cannot be read
    are skipped silently.

    Iter 1: Uses frontmatter ``description:`` as the primary header source,
            falling back to the first markdown heading.
    Iter 3: Results are sorted newest-first (by mtime) and capped at
            :data:`MAX_SCAN_FILES` to match the reference implementation.
    """
    candidates = _candidate_paths(
        repo_root,
        global_roots=global_roots if global_roots is not None else DEFAULT_GLOBAL_ROOTS,
        user_roots=user_roots if user_roots is not None else DEFAULT_USER_ROOTS,
        project_paths=project_paths if project_paths is not None else DEFAULT_PROJECT_PATHS,
        local_paths=local_paths if local_paths is not None else DEFAULT_LOCAL_PATHS,
    )
    seen: set = set()
    records: list = []  # (mtime_ms, path, layer, header)
    for path, layer in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            stat = path.stat()
            mtime_ms = stat.st_mtime * 1000.0
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Iter 1: prefer frontmatter description over first heading.
        description, _ = _parse_frontmatter(text)
        header = (description or _first_header(text))[:160]
        records.append((mtime_ms, path, layer, header))
    # Iter 3: sort newest-first, cap at MAX_SCAN_FILES.
    records.sort(key=lambda r: r[0], reverse=True)
    return [
        (path, layer, header)
        for _, path, layer, header in records[:MAX_SCAN_FILES]
    ]


# ---------------------------------------------------------------------------
# Iter 5: MEMORY.md index-file support
# ---------------------------------------------------------------------------


def truncate_entrypoint_content(raw: str) -> dict:
    """Truncate MEMORY.md content to the line AND byte caps.

    Returns a dict with::

        content            – truncated (or original) text
        line_count         – total source line count
        byte_count         – total source byte count (UTF-8)
        was_line_truncated – True if line cap fired
        was_byte_truncated – True if byte cap fired

    Line-truncates first (natural boundary), then byte-truncates at the last
    newline before the cap to avoid cutting mid-line. Appends a warning that
    names the fired cap so both the model and operators can diagnose overgrown
    index files.
    Mirrors the TypeScript ``truncateEntrypointContent()`` function.
    """
    trimmed = raw.strip()
    content_lines = trimmed.split("\n")
    line_count = len(content_lines)
    byte_count = len(trimmed.encode("utf-8"))
    was_line_truncated = line_count > MAX_ENTRYPOINT_LINES
    was_byte_truncated = byte_count > MAX_ENTRYPOINT_BYTES

    if not was_line_truncated and not was_byte_truncated:
        return {
            "content": trimmed,
            "line_count": line_count,
            "byte_count": byte_count,
            "was_line_truncated": False,
            "was_byte_truncated": False,
        }

    truncated = (
        "\n".join(content_lines[:MAX_ENTRYPOINT_LINES])
        if was_line_truncated
        else trimmed
    )
    # Secondary byte cap: cut at last newline before the byte limit.
    truncated_bytes = truncated.encode("utf-8")
    if len(truncated_bytes) > MAX_ENTRYPOINT_BYTES:
        clipped = truncated_bytes[:MAX_ENTRYPOINT_BYTES]
        last_nl = clipped.rfind(b"\n")
        cutoff = last_nl if last_nl > 0 else MAX_ENTRYPOINT_BYTES
        truncated = truncated_bytes[:cutoff].decode("utf-8", errors="replace")

    if was_byte_truncated and not was_line_truncated:
        reason = (
            f"{byte_count:,} bytes (limit: {MAX_ENTRYPOINT_BYTES:,})"
            " — index entries are too long"
        )
    elif was_line_truncated and not was_byte_truncated:
        reason = f"{line_count} lines (limit: {MAX_ENTRYPOINT_LINES})"
    else:
        reason = f"{line_count} lines and {byte_count:,} bytes"

    warning = (
        f"\n\n> WARNING: {ENTRYPOINT_NAME} is {reason}. Only part of it was loaded. "
        "Keep index entries to one line under ~200 chars; move detail into topic files."
    )
    return {
        "content": truncated + warning,
        "line_count": line_count,
        "byte_count": byte_count,
        "was_line_truncated": was_line_truncated,
        "was_byte_truncated": was_byte_truncated,
    }


# ---------------------------------------------------------------------------
# Iter 6: Auto-memory directory management
# ---------------------------------------------------------------------------


def ensure_memory_dir_exists(path) -> bool:
    """Create *path* (and any parents) if it does not already exist.

    Idempotent — EEXIST is silently swallowed. Returns ``True`` on success,
    ``False`` on permission/filesystem errors.
    Mirrors the TypeScript ``ensureMemoryDirExists()`` helper.
    """
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Iter 7: Rich memory-header scanning and manifest formatting
# ---------------------------------------------------------------------------


def _iso_ms(mtime_ms: float) -> str:
    """Return an ISO-8601 UTC timestamp string for a millisecond epoch value."""
    import datetime
    dt = datetime.datetime.fromtimestamp(
        mtime_ms / 1000.0, tz=datetime.timezone.utc
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_memory_headers(memory_dir, *, signal=None) -> list:
    """Scan *memory_dir* for ``.md`` files (excluding ``MEMORY.md``).

    Returns a list of :class:`MemoryHeader` objects sorted newest-first,
    capped at :data:`MAX_SCAN_FILES`. Failed reads are silently skipped.

    ``signal`` is accepted (and ignored) for API symmetry with the TypeScript
    ``scanMemoryFiles(memoryDir, signal)`` signature.
    Mirrors the TypeScript ``scanMemoryFiles()`` implementation.
    """
    memory_dir = Path(memory_dir)
    try:
        entries = list(memory_dir.rglob("*.md"))
    except OSError:
        return []
    headers: list = []
    for entry in entries:
        if entry.name == ENTRYPOINT_NAME:
            continue
        try:
            stat = entry.stat()
            mtime_ms = stat.st_mtime * 1000.0
            # Read only the first FRONTMATTER_MAX_LINES for efficiency.
            lines_buf: list = []
            with entry.open(encoding="utf-8", errors="replace") as fh:
                for _ in range(FRONTMATTER_MAX_LINES):
                    line = fh.readline()
                    if not line:
                        break
                    lines_buf.append(line)
            head_text = "".join(lines_buf)
            description, mem_type = _parse_frontmatter(head_text)
            if not description:
                description = _first_header(head_text)[:160]
            try:
                filename = str(entry.relative_to(memory_dir))
            except ValueError:
                filename = entry.name
            headers.append(
                MemoryHeader(
                    filename=filename,
                    file_path=entry,
                    mtime_ms=mtime_ms,
                    description=description,
                    mem_type=mem_type,
                )
            )
        except OSError:
            continue
    headers.sort(key=lambda h: h.mtime_ms, reverse=True)
    return headers[:MAX_SCAN_FILES]


def format_memory_manifest(headers: list) -> str:
    """Format memory headers as a one-line-per-file text manifest.

    Format: ``- [type] filename (ISO-timestamp): description``

    Used by LLM-based relevance selectors. Mirrors the TypeScript
    ``formatMemoryManifest()`` implementation.
    """
    lines: list = []
    for h in headers:
        tag = f"[{h.mem_type}] " if h.mem_type else ""
        ts = _iso_ms(h.mtime_ms)
        if h.description:
            lines.append(f"- {tag}{h.filename} ({ts}): {h.description}")
        else:
            lines.append(f"- {tag}{h.filename} ({ts})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Iter 9: Security path validation
# ---------------------------------------------------------------------------


def validate_memory_path(raw) -> Optional[Path]:
    """Validate and normalise a memory directory path.

    Mirrors the TypeScript ``validateMemoryPath()`` security checks:

    - Null byte (``\\0``) — survives normalise(), can truncate in syscalls.
    - Non-absolute after expansion — relative path traversal.
    - Near-root (normalised string length < 3) — e.g. ``/``, ``/a``.
    - UNC paths (``//server`` or ``\\\\server``) — opaque trust boundary.
    - Windows drive-root only (e.g. ``C:``) — whole-drive write access.

    Returns the resolved :class:`pathlib.Path` on success, ``None`` on
    rejection.
    """
    if not raw:
        return None
    s = str(raw)
    if "\0" in s:
        return None
    candidate = Path(s).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        resolved = candidate
    if not resolved.is_absolute():
        return None
    norm = str(resolved).rstrip("/").rstrip("\\")
    if len(norm) < 3:
        return None
    if re.match(r"^[A-Za-z]:$", norm):
        return None
    rs = str(resolved)
    if rs.startswith("//") or rs.startswith("\\\\"):
        return None
    return resolved


# ---------------------------------------------------------------------------
# Iter 10: Auto-memory enable/disable gate
# ---------------------------------------------------------------------------


def is_auto_memory_enabled() -> bool:
    """Whether auto-memory features are enabled.

    Checks the ``MINI_AGENT_DISABLE_AUTO_MEMORY`` env var first:
    ``1/true/yes/on``  → disabled,
    ``0/false/no/off`` → enabled,
    absent             → enabled (default).
    Mirrors the TypeScript ``isAutoMemoryEnabled()`` helper.
    """
    val = os.environ.get("MINI_AGENT_DISABLE_AUTO_MEMORY", "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return False
    return True


# ---------------------------------------------------------------------------
# Header-based retrieval
# ---------------------------------------------------------------------------


# Layer precedence weight: local > project > user > global. The scorer adds
# this so files from more specific scopes win ties against more general ones.
LAYER_WEIGHTS = {"local": 4, "project": 3, "user": 2, "global": 1}


def _default_scorer(query, header, layer):
    """Keyword-overlap scorer used when no LLM scorer is supplied.

    Splits the query and the header into lowercase word sets and counts the
    intersection; ties broken by layer precedence. This is intentionally
    simple — production use would substitute an LLM-based scan as described
    in the architecture brief.
    """
    if not query:
        return LAYER_WEIGHTS.get(layer, 0)
    q_words = {w for w in re.split(r"\W+", query.lower()) if len(w) > 2}
    h_words = {w for w in re.split(r"\W+", header.lower()) if len(w) > 2}
    overlap = len(q_words & h_words)
    return overlap * 10 + LAYER_WEIGHTS.get(layer, 0)


def select_memory_files(
    repo_root,
    query="",
    *,
    max_files=DEFAULT_MAX_FILES,
    scorer=None,
    already_surfaced=None,
    **discover_kwargs,
):
    """Pick up to ``max_files`` memory files most relevant to ``query``.

    Parameters
    ----------
    repo_root : str | Path | None
        Workspace root used to resolve project / local files.
    query : str
        A short human-readable description of the current task. Used by
        ``scorer`` to rank candidates by header relevance.
    max_files : int
        Hard cap on the number of returned files (default 5).
    scorer : callable | None
        ``scorer(query, header, layer) -> number``. Higher is better. Defaults
        to a keyword-overlap scorer.
    already_surfaced : set | None
        Iter 8: A set of path strings (or Path objects) to exclude from
        selection, preventing re-selection of files already shown in earlier
        turns. Mirrors the TypeScript ``alreadySurfaced: ReadonlySet<string>``
        parameter in ``findRelevantMemories()``.
    **discover_kwargs : passed through to :func:`discover_memory_files`.

    Returns ``[(path, layer, header, text)]`` for the selected files.
    """
    scorer = scorer or _default_scorer
    candidates = discover_memory_files(repo_root, **discover_kwargs)
    # Iter 8: deduplicate against already-surfaced paths.
    if already_surfaced:
        surfaced_strs = {str(p) for p in already_surfaced}
        candidates = [
            (p, l, h) for p, l, h in candidates if str(p) not in surfaced_strs
        ]
    scored = []
    for path, layer, header in candidates:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scored.append((scorer(query, header, layer), path, layer, header, text))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [(path, layer, header, text) for _, path, layer, header, text in scored[:max_files]]


def render_memory_files(selected, *, header="Memory files:"):
    """Render the result of :func:`select_memory_files` as a single text
    block suitable for inclusion in the prompt's ``<project-rules>`` layer.
    """
    if not selected:
        return ""
    chunks = [header]
    for path, layer, head, text in selected:
        chunks.append(f"# {path} [{layer}]\n# header: {head}\n{text.strip()}")
    return "\n\n".join(chunks)
