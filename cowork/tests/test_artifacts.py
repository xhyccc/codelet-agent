"""Tests for the artifact engine + sanitizer."""
from __future__ import annotations

from pathlib import Path

from cowork.artifacts import ArtifactEngine, parse_artifacts, sanitize_html, wrap_iframe_document


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parse_artifacts_multiple():
    raw = (
        'text <artifact type="html" title="Hello">'
        '<p>hi</p></artifact> mid '
        '<artifact type="markdown" title="Notes"># H</artifact>'
    )
    out = parse_artifacts(raw)
    assert len(out) == 2
    assert out[0][0]["type"] == "html"
    assert out[0][0]["title"] == "Hello"
    assert "<p>hi</p>" in out[0][1]
    assert out[1][0]["type"] == "markdown"


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------

def test_sanitize_strips_scripts():
    body = '<p>ok</p><script>alert(1)</script><p>x</p>'
    cleaned = sanitize_html(body)
    assert "<script" not in cleaned.lower()
    assert "alert(1)" not in cleaned
    assert "<p>ok</p>" in cleaned and "<p>x</p>" in cleaned


def test_sanitize_strips_on_handlers():
    body = '<a href="x" onclick="evil()">link</a>'
    cleaned = sanitize_html(body)
    assert "onclick" not in cleaned.lower()


def test_sanitize_blocks_javascript_uri():
    body = '<a href="javascript:alert(1)">click</a>'
    cleaned = sanitize_html(body)
    assert "javascript:" not in cleaned.lower()
    assert "#blocked" in cleaned


def test_sanitize_strips_iframes_and_objects():
    body = '<iframe src="x"></iframe><object data="y"></object><embed src="z">'
    cleaned = sanitize_html(body)
    low = cleaned.lower()
    assert "<iframe" not in low
    assert "<object" not in low
    assert "<embed" not in low


def test_sanitize_is_idempotent():
    body = '<p>x</p><script>1</script>'
    once = sanitize_html(body)
    twice = sanitize_html(once)
    assert once == twice


def test_wrap_iframe_document():
    out = wrap_iframe_document("<p>hi</p>", title="Demo & test")
    assert out.startswith("<!doctype html>")
    assert "Demo &amp; test" in out
    assert "<p>hi</p>" in out


# ---------------------------------------------------------------------------
# Engine.store + ingest
# ---------------------------------------------------------------------------

def test_engine_store_html_persists_safely(tmp_path: Path):
    eng = ArtifactEngine(tmp_path)
    rec = eng.store({"type": "html", "title": "My Page"}, "<p>ok</p><script>x</script>")
    assert rec.kind == "html"
    assert rec.sanitized
    persisted = (tmp_path / rec.path).read_text()
    assert "<script" not in persisted.lower()
    assert "<p>ok</p>" in persisted
    assert persisted.startswith("<!doctype html>")


def test_engine_store_markdown_no_sanitize(tmp_path: Path):
    eng = ArtifactEngine(tmp_path)
    rec = eng.store({"type": "markdown", "title": "Notes"}, "# Title\n\nbody")
    assert rec.kind == "markdown"
    assert not rec.sanitized
    assert (tmp_path / rec.path).read_text().startswith("# Title")


def test_engine_unknown_kind_falls_back_to_code(tmp_path: Path):
    eng = ArtifactEngine(tmp_path)
    rec = eng.store({"type": "binary"}, "raw")
    assert rec.kind == "code"


def test_engine_ingest_stream(tmp_path: Path):
    eng = ArtifactEngine(tmp_path)
    raw = (
        'pre <artifact type="html" title="A"><p>1</p></artifact> '
        '<artifact type="markdown" title="B">b</artifact>'
    )
    records = eng.ingest(raw)
    assert len(records) == 2
    kinds = sorted(r.kind for r in records)
    assert kinds == ["html", "markdown"]
    for r in records:
        assert (tmp_path / r.path).exists()
