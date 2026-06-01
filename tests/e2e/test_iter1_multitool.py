"""Iteration 1 e2e: multi-tool batching + concurrency-safe parallel execution."""

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
)


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


def test_two_read_tools_run_in_one_turn(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>\n'
        '<tool>{"name":"read_file","args":{"path":"b.txt"}}</tool>',
        "<final>both files read</final>",
    ]
    agent, client = build_agent(tmp_path, outputs)

    answer = agent.ask("read both files")

    assert answer == "both files read"
    events = _tool_events(agent)
    # Both reads executed even though the model only spoke twice (one batched
    # tool turn + one final turn).
    assert len(events) == 2
    assert [e["name"] for e in events] == ["read_file", "read_file"]
    assert "alpha" in events[0]["content"]
    assert "beta" in events[1]["content"]
    assert len(client.prompts) == 2


def test_batch_preserves_order_with_mixed_safety(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>\n'
        '<tool>{"name":"list_files","args":{"path":"."}}</tool>\n'
        '<tool>{"name":"search","args":{"pattern":"alpha","path":"."}}</tool>',
        "<final>done</final>",
    ]
    agent, client = build_agent(tmp_path, outputs)

    answer = agent.ask("inspect")

    assert answer == "done"
    events = _tool_events(agent)
    assert [e["name"] for e in events] == ["read_file", "list_files", "search"]


def test_single_tool_turn_still_works(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>',
        "<final>read</final>",
    ]
    agent, client = build_agent(tmp_path, outputs)

    answer = agent.ask("read a")

    assert answer == "read"
    events = _tool_events(agent)
    assert len(events) == 1
    assert events[0]["name"] == "read_file"
