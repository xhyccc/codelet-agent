"""Slash commands for codelet.

Mirrors the reference agent's commands.ts:
- Registry of slash commands
- Built-in commands: /compact, /memory, /skills, /plan, /cost
- Commands can be discovered from skills
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional


class Command:
    """A slash command."""

    def __init__(
        self,
        name: str,
        description: str,
        handler: Callable[["codelet.agent.MiniAgent", str], str],
    ):
        self.name = name
        self.description = description
        self.handler = handler


def _compact_command(agent, args):
    """/compact - trigger compaction cascade."""
    from . import compaction as compaction_module
    history = agent.session["history"]
    target = agent.config.get("harness", {}).get("compaction", {}).get(
        "target_chars", compaction_module.DEFAULT_COMPACTION["target_chars"]
    )
    rendered = compaction_module.render_history_size(history)
    if rendered <= target:
        return f"History is {rendered} chars (target {target}) — no compaction needed."
    try:
        outcome = compaction_module.run_cascade(
            history,
            current_budget=agent.config.get("harness", {}).get("max_tool_output", 4000),
            config=agent.config.get("harness", {}).get("compaction") or {},
            model_client=agent.model_client if agent.config.get("harness", {}).get("compaction", {}).get("auto_compaction", True) else None,
            autocompact_tokens=agent.config.get("harness", {}).get("compaction", {}).get("autocompact_tokens", 512),
            autocompact_prompt=(agent.config.get("prompts") or {}).get("autocompact"),
        )
    except compaction_module.HardHaltError as exc:
        return f"Compaction halted: {exc}"
    stages = ", ".join(outcome.get("stages_applied", []))
    return f"Compacted history from {rendered} to {compaction_module.render_history_size(outcome['history'])} chars. Stages: {stages}."


def _memory_command(agent, args):
    """/memory - show working memory."""
    memory = agent.session["memory"]
    lines = ["<memory>"]
    if memory.get("task"):
        lines.append(f"Task: {memory['task']}")
    if memory.get("files"):
        lines.append(f"Files: {', '.join(memory['files'])}")
    if memory.get("notes"):
        lines.append("Notes:")
        for note in memory["notes"]:
            lines.append(f"  - {note}")
    lines.append("</memory>")
    return "\n".join(lines)


def _cost_command(agent, args):
    """/cost - show accumulated cost and usage."""
    return agent.cost_tracker.format_summary()


def _skills_command(agent, args):
    """/skills - list available skills."""
    from . import skills as skills_module
    skills = skills_module.discover_skills(agent.workspace.repo_root)
    if not skills:
        return "No skills discovered."
    lines = ["Available skills:"]
    for skill in skills:
        lines.append(f"  - {skill.name}: {skill.description}")
    return "\n".join(lines)


def _plan_command(agent, args):
    """/plan - create or show the active plan."""
    plan = agent.session.get("plan")
    if args.strip():
        # Create a new plan from the argument
        steps = [s.strip() for s in args.strip().split(",") if s.strip()]
        if not steps:
            steps = [args.strip()]
        agent.session["plan"] = {"goal": args.strip(), "steps": steps}
        agent.session_store.save(agent.session)
        return f"Plan created with {len(steps)} step(s): {', '.join(steps)}"
    if not plan:
        return "No active plan. Use /plan <goal> to create one."
    lines = [f"Active plan: {plan.get('goal', '')}"]
    for i, step in enumerate(plan.get("steps", []), 1):
        lines.append(f"  {i}. {step}")
    return "\n".join(lines)


BUILTIN_COMMANDS: Dict[str, Command] = {
    "compact": Command("compact", "Trigger context compaction", _compact_command),
    "memory": Command("memory", "Show working memory", _memory_command),
    "cost": Command("cost", "Show accumulated cost and usage", _cost_command),
    "skills": Command("skills", "List available skills", _skills_command),
    "plan": Command("plan", "Create or show the active plan", _plan_command),
}


def get_commands(agent) -> Dict[str, Command]:
    """Return all available commands for an agent."""
    # Start with builtins
    commands = dict(BUILTIN_COMMANDS)
    # TODO: add skill-derived commands
    return commands


def run_command(agent, text: str) -> str:
    """Parse and execute a slash command.

    ``text`` should start with ``/`` followed by the command name and
    optional arguments, e.g. ``/compact`` or ``/plan do A, then B``.
    """
    text = text.strip()
    if not text.startswith("/"):
        return f"error: not a slash command: {text}"
    parts = text[1:].split(None, 1)
    name = parts[0] if parts else ""
    args = parts[1] if len(parts) > 1 else ""
    commands = get_commands(agent)
    cmd = commands.get(name)
    if cmd is None:
        return f"error: unknown command /{name}. Available: {', '.join(commands)}"
    return cmd.handler(agent, args)
