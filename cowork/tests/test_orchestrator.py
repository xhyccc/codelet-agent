"""Tests for the multi-agent orchestrator."""
from __future__ import annotations

import pytest

from cowork.collab import FileLockManager
from cowork.orchestrator import (
    HierarchicalOrchestrator,
    SequentialOrchestrator,
    SwarmOrchestrator,
    Task,
)


def echo_runner(task_id, prompt, ctx):
    return f"{task_id}:{prompt}|ctx={sorted(ctx.keys())}"


# ---------------------------------------------------------------------------
# Hierarchical
# ---------------------------------------------------------------------------

def test_hierarchical_fans_out_and_aggregates():
    calls = []

    def runner(tid, prompt, ctx):
        calls.append((tid, prompt))
        if tid == "lead":
            return f"agg:{len(ctx['worker_results'])}"
        return f"out:{tid}"

    orch = HierarchicalOrchestrator(runner)
    res = orch.run(
        "summarize",
        [Task(id="w1", prompt="a"), Task(id="w2", prompt="b")],
    )
    assert res["lead_output"] == "agg:2"
    assert [r.output for r in res["workers"]] == ["out:w1", "out:w2"]
    assert ("lead", "summarize") in calls


def test_hierarchical_records_worker_error():
    def runner(tid, prompt, ctx):
        if tid == "w1":
            raise RuntimeError("boom")
        if tid == "lead":
            return "ok"
        return "fine"

    orch = HierarchicalOrchestrator(runner)
    res = orch.run("p", [Task(id="w1", prompt="x"), Task(id="w2", prompt="y")])
    workers = {r.task_id: r for r in res["workers"]}
    assert workers["w1"].error == "boom"
    assert workers["w2"].error is None


# ---------------------------------------------------------------------------
# Sequential / DAG
# ---------------------------------------------------------------------------

def test_sequential_respects_dependencies():
    order: list[str] = []

    def runner(tid, prompt, ctx):
        order.append(tid)
        return f"r:{tid}"

    tasks = [
        Task(id="a", prompt="p_a"),
        Task(id="b", prompt="p_b", depends_on=["a"]),
        Task(id="c", prompt="p_c", depends_on=["a"]),
        Task(id="d", prompt="p_d", depends_on=["b", "c"]),
    ]
    res = SequentialOrchestrator(runner).run(tasks)
    assert order.index("a") < order.index("b") < order.index("d")
    assert order.index("a") < order.index("c") < order.index("d")
    assert res["d"].output == "r:d"


def test_sequential_forwards_upstream_outputs():
    seen: dict[str, dict] = {}

    def runner(tid, prompt, ctx):
        seen[tid] = ctx
        return f"out_{tid}"

    tasks = [Task(id="a", prompt="x"), Task(id="b", prompt="y", depends_on=["a"])]
    SequentialOrchestrator(runner).run(tasks)
    assert seen["b"]["upstream"] == {"a": "out_a"}


def test_sequential_stops_on_failure():
    def runner(tid, prompt, ctx):
        if tid == "a":
            raise ValueError("nope")
        return "ok"

    tasks = [Task(id="a", prompt="x"), Task(id="b", prompt="y", depends_on=["a"])]
    res = SequentialOrchestrator(runner).run(tasks)
    assert res["a"].error == "nope"
    # "b" depends on the failed "a"; it should be present but marked blocked.
    assert "b" in res
    assert res["b"].error is not None
    assert "blocked" in res["b"].error


def test_sequential_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        SequentialOrchestrator(echo_runner).run([Task(id="x", prompt="a"), Task(id="x", prompt="b")])


# ---------------------------------------------------------------------------
# Swarm
# ---------------------------------------------------------------------------

def test_swarm_claims_atomically_and_completes_all():
    sw = SwarmOrchestrator("ws1", FileLockManager(default_ttl=60))
    for i in range(5):
        sw.add(Task(id=f"t{i}", prompt=f"p{i}"))

    def runner(tid, prompt, ctx):
        return f"done:{tid}"

    cards = sw.run(runner, workers=["agent-a", "agent-b"])
    assert sw.stats()["done"] == 5
    assert all(c.output.startswith("done:") for c in cards.values())


def test_swarm_claim_does_not_double_assign():
    sw = SwarmOrchestrator("ws1")
    sw.add(Task(id="t1", prompt="p"))
    sw.add(Task(id="t2", prompt="p"))
    c1 = sw.claim("a")
    c2 = sw.claim("b")
    assert c1 is not None and c2 is not None
    assert {c1.task.id, c2.task.id} == {"t1", "t2"}
    # Nothing else pending
    assert sw.claim("c") is None


def test_swarm_records_failures():
    sw = SwarmOrchestrator("ws1")
    sw.add(Task(id="t1", prompt="p"))

    def runner(tid, prompt, ctx):
        raise RuntimeError("x")

    sw.run(runner, workers=["a"])
    assert sw.stats()["failed"] == 1
    assert sw.cards["t1"].error == "x"
