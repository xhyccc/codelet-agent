"""Multi-agent orchestrator.

Three coordination modes:

* ``HierarchicalOrchestrator`` — a lead agent fans tasks out to workers and
  aggregates their results. Workers run sequentially against an injected
  ``runner`` callable (lets us swap the real :class:`cowork.engine.CodeletEngine`
  for a mock in tests).
* ``SequentialOrchestrator`` — DAG executed via ``graphlib.TopologicalSorter``;
  tasks declare dependencies and the orchestrator passes upstream results into
  downstream task contexts.
* ``SwarmOrchestrator`` — shared kanban board; agents call ``claim()`` to
  atomically pick up the next pending task. The atomic claim is backed by the
  F4 :class:`FileLockManager` so it's the same primitive the production-bound
  Redis adapter will use.
"""
from __future__ import annotations

import graphlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .collab import FileLockManager, LockError


# A runner takes (task_id, prompt, context) and returns a string result.
Runner = Callable[[str, str, dict[str, Any]], str]


# ---------------------------------------------------------------------------
# Task primitives
# ---------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    output: str
    worker: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Hierarchical
# ---------------------------------------------------------------------------

class HierarchicalOrchestrator:
    """Lead -> workers fan-out, sequential collection."""

    def __init__(self, runner: Runner, *, lead_id: str = "lead"):
        self.runner = runner
        self.lead_id = lead_id

    def run(self, lead_prompt: str, subtasks: Iterable[Task]) -> dict[str, Any]:
        worker_results: list[TaskResult] = []
        for t in subtasks:
            try:
                out = self.runner(t.id, t.prompt, t.context)
                worker_results.append(TaskResult(task_id=t.id, output=out, worker=t.id))
            except Exception as e:  # noqa: BLE001
                worker_results.append(TaskResult(task_id=t.id, output="", worker=t.id, error=str(e)))
        # Lead aggregates.
        aggregate_ctx = {
            "worker_results": [
                {"id": r.task_id, "output": r.output, "error": r.error} for r in worker_results
            ]
        }
        lead_output = self.runner(self.lead_id, lead_prompt, aggregate_ctx)
        return {"lead_output": lead_output, "workers": worker_results}


# ---------------------------------------------------------------------------
# Sequential / DAG
# ---------------------------------------------------------------------------

class SequentialOrchestrator:
    """Execute tasks in topological order, forwarding upstream outputs."""

    def __init__(self, runner: Runner):
        self.runner = runner

    def run(self, tasks: Iterable[Task]) -> dict[str, TaskResult]:
        tasks = list(tasks)
        sorter: graphlib.TopologicalSorter[str] = graphlib.TopologicalSorter()
        by_id: dict[str, Task] = {}
        for t in tasks:
            if t.id in by_id:
                raise ValueError(f"duplicate task id: {t.id}")
            by_id[t.id] = t
            sorter.add(t.id, *t.depends_on)
        results: dict[str, TaskResult] = {}
        for tid in sorter.static_order():
            t = by_id[tid]
            ctx = dict(t.context)
            ctx["upstream"] = {
                d: results[d].output for d in t.depends_on if d in results
            }
            try:
                out = self.runner(t.id, t.prompt, ctx)
                results[t.id] = TaskResult(task_id=t.id, output=out)
            except Exception as e:  # noqa: BLE001
                results[t.id] = TaskResult(task_id=t.id, output="", error=str(e))
                # Stop on first failure -- downstream tasks would have empty inputs.
                break
        return results


# ---------------------------------------------------------------------------
# Swarm (shared kanban)
# ---------------------------------------------------------------------------

@dataclass
class KanbanCard:
    task: Task
    status: str = "pending"  # pending|claimed|done|failed
    worker: Optional[str] = None
    output: str = ""
    error: Optional[str] = None


class SwarmOrchestrator:
    """Atomic-claim kanban driven by FileLockManager.

    Each card maps to a ``(workspace_id, task:<id>)`` lock; workers call
    :meth:`claim` to grab the first pending card.
    """

    def __init__(self, workspace_id: str, lock_manager: Optional[FileLockManager] = None):
        self.workspace_id = workspace_id
        self.locks = lock_manager or FileLockManager(default_ttl=60)
        self.cards: dict[str, KanbanCard] = {}

    def add(self, task: Task) -> None:
        self.cards[task.id] = KanbanCard(task=task)

    def claim(self, worker: str) -> Optional[KanbanCard]:
        for card in self.cards.values():
            if card.status != "pending":
                continue
            try:
                self.locks.acquire(self.workspace_id, f"task:{card.task.id}", worker)
            except LockError:
                continue
            card.status = "claimed"
            card.worker = worker
            return card
        return None

    def complete(self, task_id: str, output: str) -> None:
        card = self.cards[task_id]
        card.status = "done"
        card.output = output

    def fail(self, task_id: str, error: str) -> None:
        card = self.cards[task_id]
        card.status = "failed"
        card.error = error

    def run(self, runner: Runner, *, workers: list[str]) -> dict[str, KanbanCard]:
        """Drive the board to completion using ``workers`` round-robin."""
        wi = 0
        while True:
            pending = [c for c in self.cards.values() if c.status == "pending"]
            if not pending:
                break
            worker = workers[wi % len(workers)]
            wi += 1
            card = self.claim(worker)
            if card is None:
                break
            try:
                out = runner(card.task.id, card.task.prompt, card.task.context)
                self.complete(card.task.id, out)
            except Exception as e:  # noqa: BLE001
                self.fail(card.task.id, str(e))
        return self.cards

    def stats(self) -> dict[str, int]:
        out = {"pending": 0, "claimed": 0, "done": 0, "failed": 0}
        for c in self.cards.values():
            out[c.status] = out.get(c.status, 0) + 1
        return out
