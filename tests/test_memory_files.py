"""Tests for hierarchical filesystem-backed memory."""

from codelet.memory_files import (
    LAYER_WEIGHTS,
    discover_memory_files,
    render_memory_files,
    select_memory_files,
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
