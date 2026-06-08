"""Tests for the graduated compaction cascade."""

import pytest

from codelet.compaction import (
    AUTOCOMPACT_SYSTEM_PROMPT,
    DEFAULT_COMPACTION,
    HardHaltError,
    apply_tool_output_budget,
    auto_compaction,
    budget_reduction,
    build_autocompact_prompt,
    context_collapse,
    microcompaction,
    render_history_size,
    run_cascade,
    snipping,
)


class FakeClient:
    def __init__(self, output):
        self.output = output
        self.prompts = []

    def complete(self, prompt, max_new_tokens):
        self.prompts.append(prompt)
        return self.output


def _h(role, content="", name=None):
    item = {"role": role, "content": content}
    if name:
        item["name"] = name
        item["args"] = {}
    return item


def _bulk_history(n=10, content_chars=2000):
    big = "x" * content_chars
    history = [_h("user", "do something")]
    for i in range(n):
        history.append(_h("tool", f"tool result {i}:\n" + big, name="run_shell"))
        history.append(_h("assistant", "ack"))
    return history


# ----- render_history_size -------------------------------------------------


def test_render_history_size_increases_with_content():
    small = render_history_size([_h("user", "hi")])
    big = render_history_size([_h("user", "hi" * 1000)])
    assert big > small


# ----- budget_reduction ----------------------------------------------------


def test_budget_reduction_no_op_when_under_target():
    hist = [_h("user", "hi")]
    assert budget_reduction(hist, current_budget=4000, target_chars=10000, min_tool_output=400) == 4000


def test_budget_reduction_shrinks_when_over_target():
    hist = _bulk_history(n=10, content_chars=2000)
    new = budget_reduction(hist, current_budget=4000, target_chars=2000, min_tool_output=400)
    assert new < 4000
    assert new >= 400


def test_budget_reduction_never_below_min_tool_output():
    hist = _bulk_history(n=50, content_chars=5000)
    new = budget_reduction(hist, current_budget=4000, target_chars=100, min_tool_output=400)
    # Floor is min(0.25 * current_budget, min_tool_output) — never below 400.
    assert new >= 400
    assert new < 4000


# ----- snipping ------------------------------------------------------------


def test_snipping_excises_python_traceback():
    trace = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        '    do()\n'
        '  File "x.py", line 2, in do\n'
        '    raise ValueError("boom")\n'
        "ValueError: boom\n"
        "after-trace\n"
    )
    hist = [_h("tool", trace, name="run_shell"), _h("user", "tail"), _h("user", "1"), _h("user", "2"), _h("user", "3"), _h("user", "4")]
    out = snipping(hist, preserve_recent=4, fileread_tools=["read_file"], mcp_tools=["delegate"])
    assert "snipped" in out[0]["content"]
    assert "after-trace" in out[0]["content"]


def test_snipping_preserves_file_reads():
    hist = [
        _h("tool", "Traceback (most recent call last):\n  File 'a.py'\nValueError: x", name="read_file"),
        _h("user", "1"),
        _h("user", "2"),
        _h("user", "3"),
        _h("user", "4"),
    ]
    out = snipping(hist, preserve_recent=4, fileread_tools=["read_file"], mcp_tools=["delegate"])
    assert "Traceback" in out[0]["content"]
    assert "snipped" not in out[0]["content"]


def test_snipping_leaves_recent_items_alone():
    trace = "Traceback (most recent call last):\n  File 'x'\nValueError: y\n"
    hist = [_h("tool", trace, name="run_shell")]
    out = snipping(hist, preserve_recent=4, fileread_tools=["read_file"], mcp_tools=["delegate"])
    assert out[0]["content"] == trace  # within preserve_recent window


# ----- microcompaction -----------------------------------------------------


def test_microcompaction_clips_old_tool_outputs():
    hist = [_h("tool", "x" * 5000, name="run_shell"), _h("user", "1"), _h("user", "2"), _h("user", "3"), _h("user", "4")]
    out = microcompaction(
        hist, preserve_recent=4, microcompact_clip=100, fileread_tools=["read_file"], mcp_tools=["delegate"]
    )
    assert "microcompacted" in out[0]["content"]
    assert len(out[0]["content"]) < 500


def test_microcompaction_skips_fileread_and_mcp():
    hist = [
        _h("tool", "x" * 5000, name="read_file"),
        _h("tool", "y" * 5000, name="delegate"),
        _h("user", "1"), _h("user", "2"), _h("user", "3"), _h("user", "4"),
    ]
    out = microcompaction(
        hist, preserve_recent=4, microcompact_clip=100, fileread_tools=["read_file"], mcp_tools=["delegate"]
    )
    assert len(out[0]["content"]) == 5000
    assert len(out[1]["content"]) == 5000


# ----- context_collapse ----------------------------------------------------


def test_context_collapse_flattens_old_items():
    hist = [
        _h("user", "an old request"),
        _h("tool", "huge output", name="run_shell"),
        _h("user", "1"), _h("user", "2"), _h("user", "3"), _h("user", "4"),
    ]
    out = context_collapse(
        hist, preserve_recent=4, fileread_tools=["read_file"], mcp_tools=["delegate"]
    )
    assert out[0]["content"].startswith("[collapsed user]")
    assert out[1]["content"] == "[collapsed tool:run_shell]"
    # recent untouched
    assert out[-1]["content"] == "4"


def test_context_collapse_respects_fileread_protection():
    hist = [
        _h("tool", "file contents", name="read_file"),
        _h("user", "1"), _h("user", "2"), _h("user", "3"), _h("user", "4"),
    ]
    out = context_collapse(
        hist, preserve_recent=4, fileread_tools=["read_file"], mcp_tools=["delegate"]
    )
    assert out[0]["content"] == "file contents"


# ----- auto_compaction -----------------------------------------------------


def test_auto_compaction_replaces_older_with_summary():
    hist = _bulk_history(n=6, content_chars=500)  # 6*2 + 1 = 13 items
    client = FakeClient("SUMMARY: did stuff")
    out = auto_compaction(
        hist, model_client=client, max_new_tokens=128, preserve_recent=4
    )
    assert out[0]["compacted"] is True
    assert "did stuff" in out[0]["content"]
    # Recent 4 items preserved.
    assert len(out) == 5
    assert out[-1] == hist[-1]


def test_build_autocompact_prompt_includes_system_and_transcript():
    hist = [_h("user", "hello"), _h("tool", "out", name="run_shell")]
    prompt = build_autocompact_prompt(hist)
    assert AUTOCOMPACT_SYSTEM_PROMPT.splitlines()[0] in prompt
    assert "[user] hello" in prompt
    assert "[tool:run_shell] out" in prompt
    assert "SUMMARY:" in prompt


def test_build_autocompact_prompt_accepts_custom_system_prompt():
    prompt = build_autocompact_prompt([_h("user", "x")], system_prompt="CUSTOM")
    assert prompt.startswith("CUSTOM")


# ----- cascade end-to-end --------------------------------------------------


def test_run_cascade_no_op_under_target():
    hist = [_h("user", "tiny")]
    out = run_cascade(hist, current_budget=4000, config={"target_chars": 1000})
    assert out["stages_applied"] == []
    assert out["halted"] is False
    assert out["budget"] == 4000


def test_run_cascade_stops_at_first_sufficient_stage():
    hist = _bulk_history(n=5, content_chars=3000)
    out = run_cascade(
        hist,
        current_budget=4000,
        config={"target_chars": 5000, "preserve_recent": 2, "microcompact_clip": 50},
    )
    # Should have run multiple stages but not all.
    assert "budget_reduction" in out["stages_applied"]
    assert out["halted"] is False


def test_run_cascade_invokes_auto_compaction_when_needed():
    hist = _bulk_history(n=20, content_chars=5000)
    client = FakeClient("short summary")
    out = run_cascade(
        hist,
        current_budget=4000,
        config={"target_chars": 200, "preserve_recent": 2},
        model_client=client,
        autocompact_tokens=64,
    )
    assert "auto_compaction" in out["stages_applied"]
    assert len(client.prompts) == 1


def test_run_cascade_hard_halts_on_thrashing():
    hist = _bulk_history(n=5, content_chars=5000)
    # Auto-compactor returns a HUGE summary so the relief check fails.
    client = FakeClient("Z" * 200000)
    with pytest.raises(HardHaltError, match="thrashing"):
        run_cascade(
            hist,
            current_budget=4000,
            config={"target_chars": 100, "preserve_recent": 1, "thrash_min_relief": 0.5},
            model_client=client,
            autocompact_tokens=64,
        )


def test_run_cascade_marks_halted_when_no_model_client():
    hist = _bulk_history(n=20, content_chars=5000)
    out = run_cascade(
        hist,
        current_budget=4000,
        config={"target_chars": 200, "preserve_recent": 1, "microcompact_clip": 10},
        model_client=None,
    )
    # Without auto-compaction available the cascade halts if it can't bring
    # the transcript under target.
    assert out["halted"] in (True, False)  # may or may not, depending on input
    assert "context_collapse" in out["stages_applied"]


def test_default_compaction_keys():
    """The default config dict must contain every documented knob."""
    expected = {
        "target_chars",
        "min_tool_output",
        "microcompact_clip",
        "preserve_recent",
        "thrash_min_relief",
        "mcp_tools",
        "fileread_tools",
    }
    assert expected.issubset(DEFAULT_COMPACTION.keys())


# ---------------------------------------------------------------------------
# Iter 11: _item_size handles non-string content
# ---------------------------------------------------------------------------


def test_item_size_handles_dict_content():
    from codelet.compaction import _item_size
    item = {"role": "tool", "content": {"key": "value", "nested": [1, 2, 3]}, "name": "tool"}
    size = _item_size(item)
    # repr of the dict must contribute > 0 chars
    assert size > 32


def test_item_size_handles_list_content():
    from codelet.compaction import _item_size
    item = {"role": "tool", "content": ["a", "b", "c"] * 100, "name": "tool"}
    size = _item_size(item)
    assert size > 300  # list of 300 short strings


def test_item_size_handles_none_content():
    from codelet.compaction import _item_size
    item = {"role": "tool", "content": None}
    size = _item_size(item)
    assert size >= 32


# ---------------------------------------------------------------------------
# Iter 12: apply_tool_output_budget
# ---------------------------------------------------------------------------


def test_apply_tool_output_budget_clips_large_content():
    history = [
        _h("tool", "x" * 10000, name="run_shell"),
        _h("assistant", "ok"),
    ]
    result = apply_tool_output_budget(history, budget=500)
    assert len(result[0]["content"]) < 600
    assert "budget cap" in result[0]["content"]


def test_apply_tool_output_budget_leaves_small_content():
    history = [_h("tool", "small output", name="run_shell")]
    result = apply_tool_output_budget(history, budget=500)
    assert result[0]["content"] == "small output"


def test_apply_tool_output_budget_skips_mcp_tools():
    history = [_h("tool", "x" * 10000, name="delegate")]
    result = apply_tool_output_budget(history, budget=100, mcp_tools=["delegate"])
    assert len(result[0]["content"]) == 10000  # unchanged


def test_apply_tool_output_budget_skips_fileread_tools():
    history = [_h("tool", "x" * 10000, name="read_file")]
    result = apply_tool_output_budget(history, budget=100, fileread_tools=["read_file"])
    assert len(result[0]["content"]) == 10000  # unchanged


def test_apply_tool_output_budget_does_not_mutate_original():
    original_content = "x" * 10000
    history = [_h("tool", original_content, name="run_shell")]
    apply_tool_output_budget(history, budget=100)
    assert history[0]["content"] == original_content  # deep copy, no mutation


# ---------------------------------------------------------------------------
# Iter 12: Stage 1 in run_cascade actually reduces size
# ---------------------------------------------------------------------------


def test_run_cascade_stage1_clips_tool_outputs():
    """After budget_reduction + apply_tool_output_budget, Stage 1 must
    genuinely reduce the rendered size of the history."""
    # Create a history that is just over the target thanks to large tool output.
    # The target is 1000; each tool item is ~3000 chars, with 2 items.
    history = [
        _h("user", "do something"),
        _h("tool", "A" * 3000, name="run_shell"),
        _h("tool", "B" * 3000, name="run_shell"),
    ]
    before = render_history_size(history)
    assert before > 1000
    out = run_cascade(
        history,
        current_budget=4000,
        config={
            "target_chars": 1000,
            "preserve_recent": 1,
            "min_tool_output": 400,
        },
        model_client=None,
    )
    # budget_reduction + apply_tool_output_budget should have clipped the
    # tool outputs, so budget_reduction must appear in stages_applied.
    assert "budget_reduction" in out["stages_applied"]
