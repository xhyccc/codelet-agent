"""Iteration 3 e2e: no-progress / diminishing-returns circuit breaker."""

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
)
from codelet.stop_reason import StopReason


def build_agent(tmp_path, outputs, config=None, **kwargs):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    client = FakeModelClient(outputs)
    agent = MiniAgent(
        model_client=client,
        workspace=ws,
        session_store=store,
        approval_policy="auto",
        config=config,
        **kwargs,
    )
    return agent, client


def test_no_progress_breaker_stops_on_repeated_stubs(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    # Interleaved unchanged re-reads -> dedup stubs (non-error, but no new
    # information), which the no-progress breaker should catch.
    read_a = '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>'
    read_b = '<tool>{"name":"read_file","args":{"path":"b.txt"}}</tool>'
    outputs = [read_a, read_b, read_a, read_b, read_a, "<final>unreached</final>"]
    agent, client = build_agent(
        tmp_path,
        outputs,
        config={"harness": {"no_progress_limit": 3, "max_steps": 20}},
        max_steps=20,
    )

    answer = agent.ask("inspect repeatedly")

    assert agent.last_stop_reason == StopReason.NO_PROGRESS_GIVEUP
    assert "no new information" in answer
    # Stopped before consuming the final-answer output.
    assert len(client.prompts) == 5


def test_no_progress_breaker_does_not_fire_on_normal_work(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("gamma\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"read_file","args":{"path":"a.txt"}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"b.txt"}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"c.txt"}}</tool>',
        "<final>all read</final>",
    ]
    agent, _ = build_agent(
        tmp_path,
        outputs,
        config={"harness": {"no_progress_limit": 3, "max_steps": 20}},
        max_steps=20,
    )

    answer = agent.ask("read three distinct files")

    assert answer == "all read"
    assert agent.last_stop_reason == StopReason.FINAL
