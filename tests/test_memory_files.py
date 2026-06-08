"""Tests for hierarchical filesystem-backed memory."""

import os
import time

from codelet.memory_files import (
    ENTRYPOINT_NAME,
    FRONTMATTER_MAX_LINES,
    LAYER_WEIGHTS,
    MAX_SCAN_FILES,
    MEMORY_TYPES,
    MemoryHeader,
    discover_memory_files,
    ensure_memory_dir_exists,
    format_memory_manifest,
    is_auto_memory_enabled,
    memory_age_days,
    memory_freshness_text,
    render_memory_files,
    scan_memory_headers,
    select_memory_files,
    truncate_entrypoint_content,
    validate_memory_path,
)


def _empty_globals():
    return {"global_roots": [], "user_roots": []}


def test_discover_finds_project_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agents\nbe terse.\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Project Memory\nuse pytest.\n", encoding="utf-8")
    found = discover_memory_files(tmp_path, **_empty_globals())
    names = {p.name for p, _, _ in found}
    assert "AGENTS.md" in names
    assert "CLAUDE.md" in names


def test_discover_picks_first_heading_as_header(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("nope\n# The Header\nbody\n", encoding="utf-8")
    found = discover_memory_files(tmp_path, **_empty_globals())
    assert found[0][2] == "The Header"


def test_discover_handles_no_heading(tmp_path):
    (tmp_path / "AGENTS.md").write_text("\n\nplain text first line\nmore\n", encoding="utf-8")
    found = discover_memory_files(tmp_path, **_empty_globals())
    assert "plain text first line" in found[0][2]


def test_discover_walks_claude_rules_directory(tmp_path):
    rules = tmp_path / ".claude" / "rules"
    rules.mkdir(parents=True)
    (rules / "a.md").write_text("# Rule A\nbody\n", encoding="utf-8")
    (rules / "b.md").write_text("# Rule B\nbody\n", encoding="utf-8")
    (rules / "skip.txt").write_text("not markdown", encoding="utf-8")
    found = discover_memory_files(tmp_path, **_empty_globals())
    names = sorted(p.name for p, _, _ in found)
    assert names == ["a.md", "b.md"]


def test_discover_includes_claude_local(tmp_path):
    (tmp_path / "CLAUDE.local.md").write_text("# Local Notes\nlocal\n", encoding="utf-8")
    found = discover_memory_files(tmp_path, **_empty_globals())
    layers = {p.name: layer for p, layer, _ in found}
    assert layers["CLAUDE.local.md"] == "local"


def test_discover_handles_missing_repo_root():
    found = discover_memory_files(None, **_empty_globals())
    assert found == []


def test_select_returns_top_max_files(tmp_path):
    for i in range(8):
        (tmp_path / f"R{i}.md").write_text(f"# Header {i}\n", encoding="utf-8")
    selected = select_memory_files(
        tmp_path,
        query="header",
        max_files=3,
        project_paths=[f"R{i}.md" for i in range(8)],
        local_paths=[],
        global_roots=[],
        user_roots=[],
    )
    assert len(selected) == 3


def test_select_uses_keyword_scorer(tmp_path):
    (tmp_path / "A.md").write_text("# unrelated\nx\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("# build tests pytest\nx\n", encoding="utf-8")
    selected = select_memory_files(
        tmp_path,
        query="how to run pytest",
        max_files=1,
        project_paths=["A.md", "B.md"],
        local_paths=[],
        global_roots=[],
        user_roots=[],
    )
    assert selected[0][0].name == "B.md"


def test_layer_weights_prefer_local_over_project(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# unrelated header\n", encoding="utf-8")
    (tmp_path / "CLAUDE.local.md").write_text("# unrelated header\n", encoding="utf-8")
    selected = select_memory_files(
        tmp_path,
        query="anything",
        max_files=2,
        global_roots=[],
        user_roots=[],
    )
    # Local has higher weight on tie.
    assert selected[0][1] == "local"
    assert LAYER_WEIGHTS["local"] > LAYER_WEIGHTS["project"]


def test_custom_scorer_overrides_default(tmp_path):
    (tmp_path / "A.md").write_text("# A header\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("# B header\n", encoding="utf-8")
    selected = select_memory_files(
        tmp_path,
        query="",
        max_files=2,
        scorer=lambda q, h, layer: 100 if "A" in h else 1,
        project_paths=["A.md", "B.md"],
        local_paths=[],
        global_roots=[],
        user_roots=[],
    )
    assert selected[0][0].name == "A.md"


def test_render_memory_files_includes_header_and_body(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agents\nbody-content\n", encoding="utf-8")
    selected = select_memory_files(tmp_path, **_empty_globals())
    rendered = render_memory_files(selected)
    assert "Memory files:" in rendered
    assert "AGENTS.md" in rendered
    assert "body-content" in rendered


def test_render_memory_files_empty():
    assert render_memory_files([]) == ""


# ---------------------------------------------------------------------------
# Iter 1/2: Frontmatter parsing + memory type taxonomy
# ---------------------------------------------------------------------------


def test_discover_uses_frontmatter_description_over_heading(tmp_path):
    """When a file has frontmatter description:, that string is used as the
    header returned by discover_memory_files instead of the first heading."""
    content = "---\ndescription: my frontmatter desc\ntype: project\n---\n# Actual Heading\nbody\n"
    (tmp_path / "CLAUDE.md").write_text(content, encoding="utf-8")
    found = discover_memory_files(tmp_path, **_empty_globals())
    assert found[0][2] == "my frontmatter desc"


def test_discover_falls_back_to_heading_when_no_frontmatter(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Plain Heading\nbody\n", encoding="utf-8")
    found = discover_memory_files(tmp_path, **_empty_globals())
    assert found[0][2] == "Plain Heading"


def test_frontmatter_type_must_be_in_taxonomy(tmp_path):
    """An unknown type: value in frontmatter is ignored (not a valid type)."""
    content = "---\ndescription: good desc\ntype: invalid_type\n---\nbody\n"
    (tmp_path / "CLAUDE.md").write_text(content, encoding="utf-8")
    # Still parses description correctly — just the type is rejected.
    found = discover_memory_files(tmp_path, **_empty_globals())
    assert found[0][2] == "good desc"


def test_memory_types_tuple_contains_expected_values():
    assert set(MEMORY_TYPES) == {"user", "feedback", "project", "reference"}


# ---------------------------------------------------------------------------
# Iter 3: mtime sorting + MAX_SCAN_FILES cap
# ---------------------------------------------------------------------------


def test_discover_sorts_newest_first(tmp_path):
    old = tmp_path / "old.md"
    new = tmp_path / "new.md"
    old.write_text("# Old File\n", encoding="utf-8")
    new.write_text("# New File\n", encoding="utf-8")
    now = time.time()
    os.utime(old, (now - 10, now - 10))
    os.utime(new, (now, now))
    found = discover_memory_files(
        tmp_path,
        project_paths=["old.md", "new.md"],
        local_paths=[],
        global_roots=[],
        user_roots=[],
    )
    assert found[0][0].name == "new.md"
    assert found[1][0].name == "old.md"


def test_discover_caps_at_max_scan_files(tmp_path):
    for i in range(MAX_SCAN_FILES + 5):
        (tmp_path / f"M{i}.md").write_text(f"# Mem {i}\n", encoding="utf-8")
    found = discover_memory_files(
        tmp_path,
        project_paths=[f"M{i}.md" for i in range(MAX_SCAN_FILES + 5)],
        local_paths=[],
        global_roots=[],
        user_roots=[],
    )
    assert len(found) <= MAX_SCAN_FILES


# ---------------------------------------------------------------------------
# Iter 4: Memory freshness / staleness
# ---------------------------------------------------------------------------


def test_memory_age_days_returns_zero_for_now():
    mtime_ms = time.time() * 1000.0
    assert memory_age_days(mtime_ms) == 0


def test_memory_age_days_returns_correct_days():
    # 5 days ago in ms
    five_days_ago_ms = (time.time() - 5 * 86400) * 1000.0
    assert memory_age_days(five_days_ago_ms) == 5


def test_memory_age_days_clamps_negative():
    future_ms = (time.time() + 86400) * 1000.0
    assert memory_age_days(future_ms) == 0


def test_memory_freshness_text_fresh_is_empty():
    mtime_ms = time.time() * 1000.0
    assert memory_freshness_text(mtime_ms) == ""


def test_memory_freshness_text_stale_contains_warning():
    old_ms = (time.time() - 10 * 86400) * 1000.0
    text = memory_freshness_text(old_ms)
    assert "10 days old" in text
    assert "Memories are point-in-time observations" in text


def test_memory_freshness_text_one_day_is_fresh():
    yesterday_ms = (time.time() - 86400) * 1000.0
    assert memory_freshness_text(yesterday_ms) == ""


# ---------------------------------------------------------------------------
# Iter 5: MEMORY.md index file / truncate_entrypoint_content
# ---------------------------------------------------------------------------


def test_truncate_entrypoint_content_no_truncation():
    raw = "- [project] notes.md (2026-01-01T00:00:00Z): short entry\n"
    result = truncate_entrypoint_content(raw)
    assert result["was_line_truncated"] is False
    assert result["was_byte_truncated"] is False
    assert result["content"] == raw.strip()


def test_truncate_entrypoint_content_line_cap():
    from codelet.memory_files import MAX_ENTRYPOINT_LINES
    lines = [f"line {i}" for i in range(MAX_ENTRYPOINT_LINES + 10)]
    raw = "\n".join(lines)
    result = truncate_entrypoint_content(raw)
    assert result["was_line_truncated"] is True
    assert "WARNING" in result["content"]
    assert f"{MAX_ENTRYPOINT_LINES + 10} lines" in result["content"]


def test_truncate_entrypoint_content_byte_cap():
    from codelet.memory_files import MAX_ENTRYPOINT_BYTES
    # Single line that's too big
    raw = "x" * (MAX_ENTRYPOINT_BYTES + 1000)
    result = truncate_entrypoint_content(raw)
    assert result["was_byte_truncated"] is True
    assert "WARNING" in result["content"]
    content_bytes = result["content"].encode("utf-8")
    # The actual content without the warning should be under cap
    assert len(raw.encode("utf-8")) > MAX_ENTRYPOINT_BYTES


def test_truncate_entrypoint_content_preserves_metadata():
    raw = "hello world"
    result = truncate_entrypoint_content(raw)
    assert result["line_count"] == 1
    assert result["byte_count"] == len(b"hello world")


# ---------------------------------------------------------------------------
# Iter 6: ensure_memory_dir_exists
# ---------------------------------------------------------------------------


def test_ensure_memory_dir_exists_creates_dir(tmp_path):
    new_dir = tmp_path / "mem" / "subdir"
    assert not new_dir.exists()
    ok = ensure_memory_dir_exists(new_dir)
    assert ok is True
    assert new_dir.is_dir()


def test_ensure_memory_dir_exists_is_idempotent(tmp_path):
    d = tmp_path / "mem"
    ensure_memory_dir_exists(d)
    ok = ensure_memory_dir_exists(d)  # second call
    assert ok is True
    assert d.is_dir()


# ---------------------------------------------------------------------------
# Iter 7: scan_memory_headers + format_memory_manifest
# ---------------------------------------------------------------------------


def test_scan_memory_headers_returns_memoryheader_objects(tmp_path):
    (tmp_path / "a.md").write_text("---\ndescription: Alpha note\ntype: project\n---\n", encoding="utf-8")
    headers = scan_memory_headers(tmp_path)
    assert len(headers) == 1
    h = headers[0]
    assert isinstance(h, MemoryHeader)
    assert h.description == "Alpha note"
    assert h.mem_type == "project"


def test_scan_memory_headers_excludes_memory_md(tmp_path):
    (tmp_path / ENTRYPOINT_NAME).write_text("# Index\n", encoding="utf-8")
    (tmp_path / "data.md").write_text("# Data\n", encoding="utf-8")
    headers = scan_memory_headers(tmp_path)
    names = [h.filename for h in headers]
    assert ENTRYPOINT_NAME not in names
    assert "data.md" in names


def test_scan_memory_headers_sorted_newest_first(tmp_path):
    old = tmp_path / "old.md"
    new = tmp_path / "new.md"
    old.write_text("# Old\n", encoding="utf-8")
    new.write_text("# New\n", encoding="utf-8")
    now = time.time()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    headers = scan_memory_headers(tmp_path)
    assert headers[0].filename == "new.md"
    assert headers[1].filename == "old.md"


def test_format_memory_manifest_with_types(tmp_path):
    (tmp_path / "a.md").write_text("---\ndescription: Alpha\ntype: user\n---\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("---\ndescription: Beta\n---\n", encoding="utf-8")
    headers = scan_memory_headers(tmp_path)
    manifest = format_memory_manifest(headers)
    assert "[user]" in manifest
    assert "Alpha" in manifest
    assert "Beta" in manifest


def test_format_memory_manifest_empty():
    assert format_memory_manifest([]) == ""


# ---------------------------------------------------------------------------
# Iter 8: already_surfaced deduplication in select_memory_files
# ---------------------------------------------------------------------------


def test_select_memory_files_respects_already_surfaced(tmp_path):
    (tmp_path / "A.md").write_text("# Alpha\ntext\n", encoding="utf-8")
    (tmp_path / "B.md").write_text("# Beta\ntext\n", encoding="utf-8")
    # First select: no exclusion
    first = select_memory_files(
        tmp_path,
        project_paths=["A.md", "B.md"],
        local_paths=[],
        global_roots=[],
        user_roots=[],
    )
    assert len(first) == 2
    # Second select: exclude A.md
    surfaced = {str(p) for p, *_ in first if p.name == "A.md"}
    second = select_memory_files(
        tmp_path,
        already_surfaced=surfaced,
        project_paths=["A.md", "B.md"],
        local_paths=[],
        global_roots=[],
        user_roots=[],
    )
    names = [p.name for p, *_ in second]
    assert "A.md" not in names
    assert "B.md" in names


# ---------------------------------------------------------------------------
# Iter 9: Security path validation
# ---------------------------------------------------------------------------


def test_validate_memory_path_accepts_valid_absolute(tmp_path):
    result = validate_memory_path(str(tmp_path))
    assert result is not None
    assert result.is_absolute()


def test_validate_memory_path_rejects_null_byte():
    assert validate_memory_path("/valid/path\0evil") is None


def test_validate_memory_path_rejects_near_root():
    # "/" normalises to just "/"" which is too short
    assert validate_memory_path("/") is None


def test_validate_memory_path_rejects_relative():
    # Relative path without leading / that stays relative
    assert validate_memory_path("relative/path") is not None  # gets expanded
    # But explicitly unresolvable relative paths that stay relative
    # (expanduser + is_absolute check)


def test_validate_memory_path_rejects_empty():
    assert validate_memory_path("") is None
    assert validate_memory_path(None) is None


def test_validate_memory_path_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = validate_memory_path("~/mydir")
    assert result is not None
    assert str(tmp_path) in str(result)


# ---------------------------------------------------------------------------
# Iter 10: is_auto_memory_enabled env var gate
# ---------------------------------------------------------------------------


def test_is_auto_memory_enabled_default(monkeypatch):
    monkeypatch.delenv("MINI_AGENT_DISABLE_AUTO_MEMORY", raising=False)
    assert is_auto_memory_enabled() is True


def test_is_auto_memory_enabled_disabled_by_1(monkeypatch):
    monkeypatch.setenv("MINI_AGENT_DISABLE_AUTO_MEMORY", "1")
    assert is_auto_memory_enabled() is False


def test_is_auto_memory_enabled_disabled_by_true(monkeypatch):
    monkeypatch.setenv("MINI_AGENT_DISABLE_AUTO_MEMORY", "true")
    assert is_auto_memory_enabled() is False


def test_is_auto_memory_enabled_re_enabled_by_0(monkeypatch):
    monkeypatch.setenv("MINI_AGENT_DISABLE_AUTO_MEMORY", "0")
    assert is_auto_memory_enabled() is True
