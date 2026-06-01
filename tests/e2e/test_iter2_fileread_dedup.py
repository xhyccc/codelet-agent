"""Iteration 2 e2e: file-read dedup returns a stub for unchanged re-reads."""

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


def test_reread_unchanged_file_returns_stub(tmp_path):
    (tmp_path / "a.txt").write_text("alpha-content\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta-content\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"b.txt"}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>',
        "<final>done</final>",
    ]
    agent, _ = build_agent(tmp_path, outputs)

    answer = agent.ask("inspect files")

    assert answer == "done"
    events = _tool_events(agent)
    assert [e["name"] for e in events] == ["read_file", "read_file", "read_file"]
    # First read shows real content; third (unchanged re-read) is a stub.
    assert "alpha-content" in events[0]["content"]
    assert "alpha-content" not in events[2]["content"]
    assert "file unchanged" in events[2]["content"]


def test_reread_after_modification_returns_fresh_content(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("first-version\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>',
        '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>',
        "<final>done</final>",
    ]
    agent, _ = build_agent(tmp_path, outputs)

    # Mutate the file between the first and third reads via a callback hook.
    original_complete = agent.model_client.complete
    state = {"calls": 0}

    def patched(prompt, max_new_tokens):
        state["calls"] += 1
        if state["calls"] == 2:
            target.write_text("second-version-much-longer\n", encoding="utf-8")
        return original_complete(prompt, max_new_tokens)

    agent.model_client.complete = patched

    answer = agent.ask("inspect")

    assert answer == "done"
    events = _tool_events(agent)
    # The file changed, so the third read must return real (new) content.
    assert "second-version" in events[2]["content"]
    assert "file unchanged" not in events[2]["content"]
