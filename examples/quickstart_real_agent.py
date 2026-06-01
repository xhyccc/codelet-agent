"""End-to-end example: ask the real agent to plan + create files.

This exercises the Iteration 1-5 tuning features against a live model:

  * multi-tool batching (parallel read-only tools)
  * file-read deduplication
  * the no-progress circuit breaker
  * schema-driven argument repair
  * skill metadata + plan re-consultation (decompose)

Run it from the repository root::

    python examples/quickstart_real_agent.py

Requires a populated ``.env`` (LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL /
LLM_API_KEY). No secrets are printed.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from _runner import build_agent_from_env


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="codelet-example-"))
    print(f"[example] workspace: {workdir}")

    # Seed a couple of files so the agent has something to read in parallel.
    (workdir / "notes.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    (workdir / "todo.txt").write_text("- ship it\n", encoding="utf-8")

    agent = build_agent_from_env(workdir, max_steps=16)

    task = (
        "First call decompose to record a short plan. Then read notes.txt and "
        "todo.txt, and write a file summary.md that contains a one-line summary "
        "of each file. Finish with a <final> answer describing what you did."
    )
    answer = agent.ask(task)

    print("\n[example] final answer:\n" + answer)
    print("\n[example] stop reason:", agent.last_stop_reason)

    summary = workdir / "summary.md"
    if summary.is_file():
        print("\n[example] summary.md contents:\n" + summary.read_text(encoding="utf-8"))
    else:
        print("\n[example] (agent did not create summary.md)")


if __name__ == "__main__":
    main()
