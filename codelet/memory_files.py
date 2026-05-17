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

import re
from pathlib import Path


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


_HEADING_RE = re.compile(r"^\s*#{1,6}\s*(.+?)\s*$", re.MULTILINE)


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

    The header is the first markdown heading (or the first non-empty line),
    truncated at 160 chars. Files that cannot be read are skipped silently.
    """
    candidates = _candidate_paths(
        repo_root,
        global_roots=global_roots if global_roots is not None else DEFAULT_GLOBAL_ROOTS,
        user_roots=user_roots if user_roots is not None else DEFAULT_USER_ROOTS,
        project_paths=project_paths if project_paths is not None else DEFAULT_PROJECT_PATHS,
        local_paths=local_paths if local_paths is not None else DEFAULT_LOCAL_PATHS,
    )
    seen = set()
    out = []
    for path, layer in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = _first_header(text)[:160]
        out.append((path, layer, header))
    return out


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
        Hard cap on the number of returned files (default 5; matches the
        protocol from the Claude Code architecture).
    scorer : callable | None
        ``scorer(query, header, layer) -> number``. Higher is better. Defaults
        to a keyword-overlap scorer.
    **discover_kwargs : passed through to :func:`discover_memory_files`.

    Returns ``[(path, layer, header, text)]`` for the selected files.
    """
    scorer = scorer or _default_scorer
    candidates = discover_memory_files(repo_root, **discover_kwargs)
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
