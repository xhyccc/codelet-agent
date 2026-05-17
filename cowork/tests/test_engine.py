"""Tests for codelet output parser and subprocess engine.

The engine tests use ``python -c`` instead of real codelet to keep tests
hermetic (no LLM backend required).
"""
from __future__ import annotations

import sys
from pathlib import Path

from cowork.engine import CodeletEngine, CodeletInvocation, SessionBridge
from cowork.parser import (
    FinalAnswer,
    TextChunk,
    ToolCall,
    extract_final,
    parse_codelet_output,
)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------

def test_parse_empty():
    assert parse_codelet_output("") == []


def test_parse_text_only():
    evs = parse_codelet_output("hello world")
    assert len(evs) == 1
    assert isinstance(evs[0], TextChunk)
    assert evs[0].text == "hello world"


def test_parse_tool_with_attr_name_and_json_body():
    out = 'before <tool name="read_file">{"path": "x.py"}</tool> after'
    evs = parse_codelet_output(out)
    kinds = [type(e).__name__ for e in evs]
    assert kinds == ["TextChunk", "ToolCall", "TextChunk"]
    tool = evs[1]
    assert isinstance(tool, ToolCall)
    assert tool.name == "read_file"
    assert tool.args == {"path": "x.py"}


def test_parse_tool_with_json_only_body():
    out = '<tool>{"tool": "list_dir", "args": {"path": "."}}</tool>'
    evs = parse_codelet_output(out)
    assert len(evs) == 1
    tool = evs[0]
    assert isinstance(tool, ToolCall)
    assert tool.name == "list_dir"
    assert tool.args == {"path": "."}


def test_parse_final():
    out = "thoughts...<final>The answer is 42.</final>"
    evs = parse_codelet_output(out)
    assert any(isinstance(e, FinalAnswer) and e.text == "The answer is 42." for e in evs)
    assert extract_final(out) == "The answer is 42."


def test_parse_interleaved_order():
    out = (
        'pre <tool name="a">{"k":1}</tool> mid '
        '<tool name="b">{"k":2}</tool> '
        '<final>done</final>'
    )
    evs = parse_codelet_output(out)
    names = [
        e.name if isinstance(e, ToolCall) else (e.text.strip() if isinstance(e, TextChunk) else "FINAL")
        for e in evs
    ]
    assert names == ["pre", "a", "mid", "b", "FINAL"]


def test_parse_malformed_tool_falls_back_to_text():
    # No name attr and unparseable body -> entire tool block ignored, text passes.
    out = "before <tool>not json here</tool> after"
    evs = parse_codelet_output(out)
    # The tool block remains as raw text (it wasn't recognised as a structured event).
    assert any(isinstance(e, TextChunk) for e in evs)


# ---------------------------------------------------------------------------
# Engine: argv construction
# ---------------------------------------------------------------------------

def test_build_argv_minimal(tmp_path: Path):
    eng = CodeletEngine()
    inv = CodeletInvocation(prompt="hi", cwd=tmp_path)
    argv = eng.build_argv(inv)
    assert argv[0] == sys.executable
    assert "-m" in argv and "codelet" in argv
    assert "--no-welcome" in argv
    assert "--approval" in argv
    assert argv[argv.index("--approval") + 1] == "auto"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "hi"


def test_build_argv_resume_and_config(tmp_path: Path):
    cfg = tmp_path / "tmp.yaml"
    cfg.write_text("model: stub")
    inv = CodeletInvocation(
        prompt="go",
        cwd=tmp_path,
        approval="never",
        resume_session_id="abc",
        config_path=cfg,
        model="qwen3",
        backend="openai",
        extra_args=("--max-steps", "3"),
    )
    argv = CodeletEngine().build_argv(inv)
    assert argv[argv.index("--resume") + 1] == "abc"
    assert argv[argv.index("--config") + 1] == str(cfg)
    assert argv[argv.index("--model") + 1] == "qwen3"
    assert argv[argv.index("--backend") + 1] == "openai"
    assert "--max-steps" in argv


# ---------------------------------------------------------------------------
# Engine: real subprocess (using python -c shim instead of real codelet)
# ---------------------------------------------------------------------------

def test_engine_run_with_shim(tmp_path: Path):
    """Use a fake "module" by running a python -c that prints a codelet-like response."""
    # We override module_spec via a trick: replace ``-m codelet`` with ``-c script``.
    # Easiest: monkeypatch build_argv via subclass.
    class _Shim(CodeletEngine):
        def build_argv(self, inv):
            return [
                sys.executable,
                "-c",
                'print(\'pre <tool name="read_file">{"path": "a"}</tool> '
                'mid <final>ok</final>\')',
            ]

    res = _Shim().run(CodeletInvocation(prompt="x", cwd=tmp_path))
    assert res.returncode == 0
    assert res.final == "ok"
    tool_events = [e for e in res.events if isinstance(e, ToolCall)]
    assert len(tool_events) == 1
    assert tool_events[0].name == "read_file"


def test_engine_run_timeout(tmp_path: Path):
    class _Shim(CodeletEngine):
        def build_argv(self, inv):
            return [sys.executable, "-c", "import time; time.sleep(5)"]

    res = _Shim().run(CodeletInvocation(prompt="x", cwd=tmp_path, timeout=0.3))
    assert res.returncode == -1
    assert "timeout" in res.stderr.lower()


def test_engine_stream_callback(tmp_path: Path):
    class _Shim(CodeletEngine):
        def build_argv(self, inv):
            return [sys.executable, "-c", "print('hello'); print('<final>done</final>')"]

    lines: list[str] = []
    res = _Shim().stream(CodeletInvocation(prompt="x", cwd=tmp_path), on_line=lines.append)
    assert res.returncode == 0
    assert res.final == "done"
    assert any("hello" in ln for ln in lines)


# ---------------------------------------------------------------------------
# Session bridge
# ---------------------------------------------------------------------------

def test_session_bridge_detect_latest(tmp_path: Path):
    sd = tmp_path / ".mini-coding-agent" / "sessions"
    sd.mkdir(parents=True)
    (sd / "older.json").write_text("{}")
    import time as _t
    _t.sleep(0.01)
    (sd / "newer.json").write_text("{}")
    bridge = SessionBridge(cowork_session_id="cs1", workspace_cwd=tmp_path)
    assert bridge.detect_latest() == "newer"


def test_session_bridge_empty_dir(tmp_path: Path):
    bridge = SessionBridge(cowork_session_id="cs1", workspace_cwd=tmp_path)
    assert bridge.detect_latest() is None
