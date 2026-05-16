"""Tests for Phase 1 foundation: StopReason, repeated-error detector,
ANSI/dedupe/clip helpers, and the checkpoint-summary compaction stage."""

import pytest

from codelet import (
    CHECKPOINT_MARKER,
    AskResult,
    FakeModelClient,
    MiniAgent,
    SessionStore,
    StopReason,
    WorkspaceContext,
    checkpoint_summary,
    clip_head_tail,
    dedupe_lines,
    has_checkpoint,
    run_cascade,
    strip_ansi,
)


# ---------- utils helpers ---------------------------------------------------


def test_strip_ansi_removes_csi_and_osc():
    raw = "\x1b[31mERROR\x1b[0m \x1b]0;title\x07normal"
    assert strip_ansi(raw) == "ERROR normal"


def test_strip_ansi_no_change_for_plain_text():
    assert strip_ansi("hello") == "hello"


def test_clip_head_tail_keeps_both_ends():
    text = "HEAD" + ("x" * 5000) + "TAIL"
    out = clip_head_tail(text, 200)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "clipped" in out
    assert len(out) < len(text)


def test_clip_head_tail_passthrough_for_short_text():
    assert clip_head_tail("short", 100) == "short"


def test_dedupe_lines_collapses_repeats():
    text = "\n".join(["A"] * 10 + ["B"])
    out = dedupe_lines(text, max_repeats=2)
    # The collapsed marker references 10 total repeats.
    assert "repeated 10 times" in out
    assert out.endswith("B")


# ---------- StopReason / AskResult ------------------------------------------


def test_stop_reason_final(tmp_path):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    client = FakeModelClient(["<final>done</final>"])
    agent = MiniAgent(model_client=client, workspace=ws, session_store=store, approval_policy="auto")
    out = agent.ask("hi")
    assert out == "done"
    assert agent.last_stop_reason is StopReason.FINAL
    assert isinstance(agent.last_ask_result, AskResult)
    assert agent.last_ask_result.tool_steps == 0


def test_stop_reason_step_limit(tmp_path):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    # Endless list_files calls; max_steps=2 so loop trips step limit.
    client = FakeModelClient([
        '<tool name="list_files"><arg name="path">.</arg></tool>',
        '<tool name="list_files"><arg name="path">.</arg></tool>',
        '<tool name="list_files"><arg name="path">.</arg></tool>',
        '<tool name="list_files"><arg name="path">.</arg></tool>',
    ])
    agent = MiniAgent(
        model_client=client, workspace=ws, session_store=store,
        approval_policy="auto", max_steps=2,
    )
    agent.ask("look around")
    # Second identical tool call will be caught by repeated-tool-call guard,
    # which returns "error: repeated ..." but is NOT an unrecoverable streak
    # of three errors yet at step 2. The loop exits via step_limit.
    assert agent.last_stop_reason in {
        StopReason.STEP_LIMIT,
        StopReason.REPEATED_ERROR_GIVEUP,
    }


def test_repeated_error_giveup(tmp_path):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    # Force the agent to repeatedly call read_file on a path that doesn't
    # exist. Each call yields an "error: ..." string and after threshold
    # consecutive errors the loop bails out.
    bad = '<tool name="read_file"><arg name="path">does_not_exist.txt</arg></tool>'
    client = FakeModelClient([bad, bad, bad, bad, bad, bad])
    agent = MiniAgent(
        model_client=client, workspace=ws, session_store=store,
        approval_policy="auto", max_steps=10,
    )
    out = agent.ask("read that file")
    assert agent.last_stop_reason is StopReason.REPEATED_ERROR_GIVEUP
    assert "Gave up after" in out


# ---------- checkpoint summary ----------------------------------------------


def _make_history(n):
    return [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"item {i}", "created_at": "t"}
        for i in range(n)
    ]


def test_checkpoint_summary_idempotent():
    history = _make_history(40)
    out = checkpoint_summary(
        history, model_client=None, preserve_recent=4, fold=20,
    )
    assert has_checkpoint(out)
    # Calling again should not add a second checkpoint.
    out2 = checkpoint_summary(
        out, model_client=None, preserve_recent=4, fold=20,
    )
    checkpoints = [item for item in out2 if item.get("checkpoint")]
    assert len(checkpoints) == 1


def test_checkpoint_summary_with_model_client():
    class Fake:
        def complete(self, prompt, n):
            return "Summary: the user wants X."

    history = _make_history(40)
    out = checkpoint_summary(
        history, model_client=Fake(), preserve_recent=4, fold=20,
    )
    assert out[0]["checkpoint"]
    assert CHECKPOINT_MARKER in out[0]["content"]
    assert "Summary: the user wants X." in out[0]["content"]
    # The 4 most recent items should be preserved verbatim.
    assert out[-1]["content"] == history[-1]["content"]


def test_cascade_invokes_checkpoint_at_watermark():
    # Build a 60-item history with bulky content so it also exceeds the
    # char target (the cascade only runs above target_chars).
    history = []
    for i in range(60):
        history.append({
            "role": "user" if i % 2 == 0 else "assistant",
            "content": "x" * 400, "created_at": "t",
        })
    config = {
        "target_chars": 8000,
        "preserve_recent": 4,
        "checkpoint_watermark": 50,
        "checkpoint_fold": 30,
    }
    outcome = run_cascade(history, current_budget=4000, config=config)
    assert "checkpoint_summary" in outcome["stages_applied"]
    assert has_checkpoint(outcome["history"])
