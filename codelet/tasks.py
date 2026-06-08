"""Task system for codelet.

Mirrors the reference agent's Task.ts:
- Spawn/kill tasks with output files and status tracking
- Support local_bash, local_agent, and other task types
- Track task status: pending -> running -> completed/failed/killed
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


TASK_STATUS = {"pending", "running", "completed", "failed", "killed"}


@dataclass
class TaskState:
    id: str
    type: str
    status: str
    description: str
    command: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    output_file: str = ""
    output_offset: int = 0
    notified: bool = False
    process: Optional[subprocess.Popen] = None
    thread: Optional[threading.Thread] = None


class TaskRegistry:
    """In-memory registry of active tasks."""

    def __init__(self, workspace_root: str):
        self.workspace_root = workspace_root
        self.tasks: Dict[str, TaskState] = {}
        self._lock = threading.Lock()

    def _output_path(self, task_id: str) -> Path:
        path = Path(self.workspace_root) / ".codelet" / "tasks" / f"{task_id}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def spawn(
        self,
        task_type: str,
        description: str,
        command: str = "",
        timeout: Optional[int] = None,
    ) -> TaskState:
        """Spawn a new task."""
        task_id = f"{task_type[0]}{uuid.uuid4().hex[:8]}"
        output_path = self._output_path(task_id)
        output_path.write_text("", encoding="utf-8")

        task = TaskState(
            id=task_id,
            type=task_type,
            status="pending",
            description=description,
            command=command,
            output_file=str(output_path),
        )

        with self._lock:
            self.tasks[task_id] = task

        if task_type == "local_bash" and command:
            task.status = "running"
            task.start_time = time.time()
            task.thread = threading.Thread(
                target=self._run_bash_task,
                args=(task, timeout or 120),
                daemon=True,
            )
            task.thread.start()

        return task

    def _run_bash_task(self, task: TaskState, timeout: int) -> None:
        """Execute a bash task and capture output."""
        try:
            with open(task.output_file, "a", encoding="utf-8") as fh:
                fh.write(f"$ {task.command}\n")
                process = subprocess.Popen(
                    task.command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=self.workspace_root,
                )
                task.process = process
                try:
                    stdout, _ = process.communicate(timeout=timeout)
                    fh.write(stdout)
                    fh.write(f"\nexit_code: {process.returncode}\n")
                    task.status = "completed" if process.returncode == 0 else "failed"
                except subprocess.TimeoutExpired:
                    process.kill()
                    fh.write("\n[timeout]\n")
                    task.status = "failed"
        except Exception as exc:
            with open(task.output_file, "a", encoding="utf-8") as fh:
                fh.write(f"\nerror: {exc}\n")
            task.status = "failed"
        finally:
            task.end_time = time.time()

    def kill(self, task_id: str) -> bool:
        """Kill a running task."""
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                return False
            if task.status in ("completed", "failed", "killed"):
                return False

            task.status = "killed"
            task.end_time = time.time()
            if task.process is not None:
                try:
                    task.process.kill()
                except Exception:
                    pass
            return True

    def get_status(self, task_id: str) -> str:
        """Get the current status of a task."""
        with self._lock:
            task = self.tasks.get(task_id)
            return task.status if task else "unknown"

    def get_output(self, task_id: str) -> str:
        """Read the current output of a task."""
        with self._lock:
            task = self.tasks.get(task_id)
        if task is None:
            return ""
        try:
            return Path(task.output_file).read_text(encoding="utf-8")
        except OSError:
            return ""

    def list_tasks(self) -> List[TaskState]:
        """List all tasks."""
        with self._lock:
            return list(self.tasks.values())


# Global registry per workspace (simplified; in production this would be
# scoped per agent or session).
_registries: Dict[str, TaskRegistry] = {}


def _get_registry(workspace_root: str) -> TaskRegistry:
    if workspace_root not in _registries:
        _registries[workspace_root] = TaskRegistry(workspace_root)
    return _registries[workspace_root]


def spawn_task(
    agent,
    task_type: str,
    description: str,
    command: str = "",
    timeout: Optional[int] = None,
) -> dict:
    """Spawn a task and return its metadata."""
    registry = _get_registry(agent.workspace.repo_root)
    task = registry.spawn(task_type, description, command, timeout)
    return {
        "id": task.id,
        "type": task.type,
        "status": task.status,
        "description": task.description,
        "output_file": task.output_file,
    }


def kill_task(task_id: str) -> bool:
    """Kill a task by ID (searches all registries)."""
    for registry in _registries.values():
        if task_id in registry.tasks:
            return registry.kill(task_id)
    return False


def get_task_status(task_id: str) -> str:
    """Get the status of a task by ID."""
    for registry in _registries.values():
        status = registry.get_status(task_id)
        if status != "unknown":
            return status
    return "unknown"
