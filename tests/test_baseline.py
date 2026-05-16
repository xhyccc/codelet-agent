"""Tests for session-baseline verification."""

from codelet.baseline import (
    capture_baseline,
    diff_baseline,
    verify_session_baseline,
)


def test_capture_baseline_returns_expected_fields(tmp_path):
    (tmp_path / "AGENTS.md").write_text("rules", encoding="utf-8")
    base = capture_baseline(tmp_path, watch_files=["AGENTS.md", "missing.md"])
    assert base["repo_root"] == str(tmp_path.resolve())
    assert "branch" in base
    assert "head_commit" in base
    assert "status_digest" in base
    assert base["files"]["AGENTS.md"] is not None
    assert base["files"]["missing.md"] is None


def test_diff_baseline_empty_when_no_previous(tmp_path):
    current = capture_baseline(tmp_path)
    assert diff_baseline(None, current) == []
    assert diff_baseline({}, current) == []


def test_diff_baseline_detects_file_appearance(tmp_path):
    before = capture_baseline(tmp_path, watch_files=["A.md"])
    (tmp_path / "A.md").write_text("hi", encoding="utf-8")
    after = capture_baseline(tmp_path, watch_files=["A.md"])
    drift = diff_baseline(before, after)
    assert any("file appeared: A.md" in line for line in drift)


def test_diff_baseline_detects_file_change(tmp_path):
    (tmp_path / "A.md").write_text("hi", encoding="utf-8")
    before = capture_baseline(tmp_path, watch_files=["A.md"])
    (tmp_path / "A.md").write_text("hello", encoding="utf-8")
    after = capture_baseline(tmp_path, watch_files=["A.md"])
    drift = diff_baseline(before, after)
    assert any("file changed on disk: A.md" in line for line in drift)


def test_diff_baseline_detects_file_disappearance(tmp_path):
    (tmp_path / "A.md").write_text("hi", encoding="utf-8")
    before = capture_baseline(tmp_path, watch_files=["A.md"])
    (tmp_path / "A.md").unlink()
    after = capture_baseline(tmp_path, watch_files=["A.md"])
    drift = diff_baseline(before, after)
    assert any("file disappeared: A.md" in line for line in drift)


def test_diff_baseline_detects_branch_change():
    before = {"repo_root": "/r", "branch": "main", "head_commit": "x", "status_digest": "y", "files": {}}
    after = {"repo_root": "/r", "branch": "feature", "head_commit": "x", "status_digest": "y", "files": {}}
    drift = diff_baseline(before, after)
    assert any("branch changed" in line for line in drift)


def test_diff_baseline_detects_head_move():
    before = {"repo_root": "/r", "branch": "main", "head_commit": "aaaa", "status_digest": "y", "files": {}}
    after = {"repo_root": "/r", "branch": "main", "head_commit": "bbbb", "status_digest": "y", "files": {}}
    drift = diff_baseline(before, after)
    assert any("HEAD moved" in line for line in drift)


def test_verify_session_baseline_seeds_first(tmp_path):
    session = {}
    drift = verify_session_baseline(session, tmp_path)
    assert drift == []
    assert "baseline" in session
    assert session["baseline"]["repo_root"] == str(tmp_path.resolve())


def test_verify_session_baseline_returns_drift_on_change(tmp_path):
    (tmp_path / "AGENTS.md").write_text("v1", encoding="utf-8")
    session = {}
    verify_session_baseline(session, tmp_path, watch_files=["AGENTS.md"])
    (tmp_path / "AGENTS.md").write_text("v2", encoding="utf-8")
    drift = verify_session_baseline(session, tmp_path, watch_files=["AGENTS.md"])
    assert any("AGENTS.md" in line for line in drift)
