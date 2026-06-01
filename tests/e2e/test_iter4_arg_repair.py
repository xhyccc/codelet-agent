"""Iteration 4 e2e: schema-driven argument repair / coercion."""

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
)
from codelet.tools import repair_tool_args


def build_agent(tmp_path, outputs, **kwargs):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    client = FakeModelClient(outputs)
    agent = MiniAgent(
        model_client=client,
        workspace=ws,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )
    return agent, client


def _tool_events(agent):
    return [item for item in agent.session["history"] if item.get("role") == "tool"]


def test_alias_key_is_repaired(tmp_path):
    (tmp_path / "a.txt").write_text("alpha-content\n", encoding="utf-8")
    outputs = [
        # "file" instead of the canonical "path".
        '<tool>{"name":"read_file","args":{"file":"a.txt"}}</tool>',
        "<final>done</final>",
    ]
    agent, _ = build_agent(tmp_path, outputs)

    answer = agent.ask("read a")

    assert answer == "done"
    events = _tool_events(agent)
    assert len(events) == 1
    assert "alpha-content" in events[0]["content"]
    assert "invalid arguments" not in events[0]["content"]


def test_string_int_args_are_coerced(tmp_path):
    (tmp_path / "a.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"read_file","args":{"path":"a.txt","start":"2","end":"3"}}</tool>',
        "<final>done</final>",
    ]
    agent, _ = build_agent(tmp_path, outputs)

    answer = agent.ask("read range")

    assert answer == "done"
    events = _tool_events(agent)
    assert "invalid" not in events[0]["content"].lower()


def test_repair_tool_args_unit():
    schema = {"path": "str", "start": "int=1", "end": "int=200"}
    out = repair_tool_args(schema, {"filename": "x.py", "start": "5", "end": 10.0})
    assert out["path"] == "x.py"
    assert out["start"] == 5 and isinstance(out["start"], int)
    assert out["end"] == 10 and isinstance(out["end"], int)
    assert "filename" not in out


def test_repair_list_coercion_unit():
    schema = {"tasks": "list[str]", "max_steps": "int=100"}
    assert repair_tool_args(schema, {"tasks": "one task"})["tasks"] == ["one task"]
    assert repair_tool_args(schema, {"tasks": "a, b, c"})["tasks"] == ["a", "b", "c"]
    assert repair_tool_args(schema, {"tasks": '["x", "y"]'})["tasks"] == ["x", "y"]
    # Aliases map onto canonical list key too.
    assert repair_tool_args(schema, {"subtasks": ["p", "q"]})["tasks"] == ["p", "q"]
