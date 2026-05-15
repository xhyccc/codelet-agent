"""Graduated context-compaction cascade.

Inspired by the leaked Claude Code harness, this module implements a five-stage
"pressure resolution" protocol that progressively reclaims tokens from the
rolling transcript when it approaches the model's context window. The stages
are applied in increasing order of aggressiveness and the cascade stops at the
first stage that brings the rendered transcript under the soft budget:

    1. budget_reduction  - lower the per-tool-output ceiling.
    2. snipping          - excise high-volume, low-value strings (stack traces,
                           repeated shell output) from older items.
    3. microcompaction   - clear intermediate outputs from iterative tool
                           calls, while preserving Model Context Protocol
                           (MCP) outputs and skipping budgeting for FileRead
                           results.
    4. context_collapse  - read-time projection that flattens older history
                           non-destructively (one summary line per item).
    5. auto_compaction   - secondary LLM call with the ``autocompact`` system
                           prompt to summarize the verbose operational history.
                           Definitive user directives, actionable task items,
                           and architectural notes are preserved verbatim.

If a single execution loop refills the context window immediately after an
auto-compaction attempt (``thrash_detected``), the engine should **hard halt**
and surface an explicit error to the user via :class:`HardHaltError` instead
of looping forever.

All stages are implemented as pure functions over a *copy* of the history
list. The history items follow the same schema produced by
:meth:`mini_coding_agent.agent.MiniAgent.record` (``role`` in
``{"user", "assistant", "tool"}``; ``tool`` items have ``name``, ``args``,
``content``).
"""

from __future__ import annotations

import re
from copy import deepcopy


# Default compaction tuning. These can be overridden via the YAML config under
# ``harness.compaction`` (see ``BUILTIN_DEFAULTS`` in :mod:`.config`).
DEFAULT_COMPACTION = {
    # Soft ceiling (in characters) for the rendered transcript. The cascade
    # runs until the rendered text drops below this number.
    "target_chars": 12000,
    # Minimum tool-output budget after :func:`budget_reduction` has trimmed.
    "min_tool_output": 400,
    # Older items get an extra-tight per-item clip during microcompaction.
    "microcompact_clip": 120,
    # Recent N items are always preserved verbatim by every stage.
    "preserve_recent": 4,
    # If the auto-compaction attempt does not free at least this fraction of
    # the budget, the engine should halt.
    "thrash_min_relief": 0.1,
    # Tool names that produce MCP outputs (schema-stable, never trimmed).
    "mcp_tools": ["delegate"],
    # Tool names that produce file-read output (skipped from budgeting so the
    # model retains full visibility of critical code).
    "fileread_tools": ["read_file"],
}


# A regex that captures contiguous Python-style stack-trace blocks. Snipping
# replaces the inside of the block with a short placeholder.
_STACK_TRACE_RE = re.compile(
    r"(Traceback \(most recent call last\):.*?)(?=\n[^ \t]|\Z)",
    re.DOTALL,
)

# Repeated shell-prompt-like noise: long banners of dashes/equals/hashes.
_BANNER_RE = re.compile(r"(?:[-=#]{20,}\s*\n){2,}")


class HardHaltError(RuntimeError):
    """Raised when the compaction cascade cannot bring the context under
    budget even after an auto-compaction attempt.

    This is the "hard halt" stage of the cascade: surfacing the failure
    explicitly to the user is preferable to continuous token thrashing.
    """


def _is_fileread(item, fileread_tools):
    return item.get("role") == "tool" and item.get("name") in fileread_tools


def _is_mcp(item, mcp_tools):
    return item.get("role") == "tool" and item.get("name") in mcp_tools


def _item_size(item):
    """Approximate the on-prompt cost of a history item (in characters)."""
    return len(str(item.get("content", ""))) + len(str(item.get("name", ""))) + 32


def render_history_size(history):
    """Sum the approximate prompt cost of every item in ``history``."""
    return sum(_item_size(item) for item in history)


# ---------------------------------------------------------------------------
# Stage 1: budget reduction
# ---------------------------------------------------------------------------


def budget_reduction(history, *, current_budget, target_chars, min_tool_output):
    """Stage 1: dynamically trim the per-tool-output budget.

    Returns the new budget (an int >= ``min_tool_output``). The caller is
    expected to apply this budget the next time it renders the transcript;
    we do not mutate the items themselves.
    """
    rendered = render_history_size(history)
    if rendered <= target_chars:
        return current_budget
    # Shrink proportionally to overflow, but never below ``min_tool_output``.
    ratio = target_chars / max(rendered, 1)
    new_budget = max(min_tool_output, int(current_budget * max(ratio, 0.25)))
    return min(current_budget, new_budget)


# ---------------------------------------------------------------------------
# Stage 2: snipping
# ---------------------------------------------------------------------------


def _snip_text(text):
    """Mathematically excise stack traces and banner noise from ``text``."""
    if not text:
        return text
    snipped = _STACK_TRACE_RE.sub(
        lambda m: "Traceback ... [snipped %d chars]" % (len(m.group(1)) - 27),
        text,
    )
    snipped = _BANNER_RE.sub("[banner snipped]\n", snipped)
    return snipped


def snipping(history, *, preserve_recent, fileread_tools, mcp_tools):
    """Stage 2: target and excise high-volume, low-value string data.

    Stack traces and repeated banner-style shell output are replaced with a
    short ``[snipped]`` placeholder. ``FileRead`` and MCP outputs are left
    untouched. The most recent ``preserve_recent`` items are also untouched.
    Returns a deep copy with the substitutions applied.
    """
    result = deepcopy(history)
    cutoff = max(0, len(result) - preserve_recent)
    for index, item in enumerate(result):
        if index >= cutoff:
            break
        if _is_fileread(item, fileread_tools) or _is_mcp(item, mcp_tools):
            continue
        if "content" in item and isinstance(item["content"], str):
            item["content"] = _snip_text(item["content"])
    return result


# ---------------------------------------------------------------------------
# Stage 3: microcompaction
# ---------------------------------------------------------------------------


def microcompaction(
    history,
    *,
    preserve_recent,
    microcompact_clip,
    fileread_tools,
    mcp_tools,
):
    """Stage 3: aggressively clear intermediate tool outputs.

    For every older tool call that is *not* an MCP output (schema must remain
    stable) and *not* a FileRead (the model needs full visibility of source),
    we replace the ``content`` with a short truncated placeholder.

    Returns a deep copy with the substitutions applied.
    """
    result = deepcopy(history)
    cutoff = max(0, len(result) - preserve_recent)
    for index, item in enumerate(result):
        if index >= cutoff:
            break
        if item.get("role") != "tool":
            continue
        if _is_fileread(item, fileread_tools):
            # Skip budgeting entirely for file reads.
            continue
        if _is_mcp(item, mcp_tools):
            # Preserve MCP outputs verbatim to maintain schema stability.
            continue
        content = str(item.get("content", ""))
        if len(content) > microcompact_clip:
            item["content"] = (
                content[:microcompact_clip].rstrip()
                + f" ... [microcompacted, was {len(content)} chars]"
            )
    return result


# ---------------------------------------------------------------------------
# Stage 4: context collapse
# ---------------------------------------------------------------------------


def context_collapse(
    history,
    *,
    preserve_recent,
    fileread_tools,
    mcp_tools,
):
    """Stage 4: read-time projection that flattens older history.

    Each older item is replaced by a one-line ``role/name`` summary so the
    model still sees the *shape* of what happened without paying the full
    token cost. FileRead and MCP outputs are exempt and pass through.

    Returns a deep copy with the substitutions applied.
    """
    result = deepcopy(history)
    cutoff = max(0, len(result) - preserve_recent)
    for index, item in enumerate(result):
        if index >= cutoff:
            break
        if _is_fileread(item, fileread_tools) or _is_mcp(item, mcp_tools):
            continue
        role = item.get("role", "?")
        if role == "tool":
            name = item.get("name", "?")
            item["content"] = f"[collapsed tool:{name}]"
        else:
            content = str(item.get("content", ""))
            head = content.strip().splitlines()[0] if content.strip() else ""
            item["content"] = f"[collapsed {role}] {head[:80]}"
    return result


# ---------------------------------------------------------------------------
# Stage 5: auto-compaction (LLM-driven)
# ---------------------------------------------------------------------------


AUTOCOMPACT_SYSTEM_PROMPT = (
    "You are the autocompact summarizer for a coding agent. You will be given\n"
    "the agent's running transcript (user requests, model thoughts, tool\n"
    "calls, tool outputs). Produce a concise summary that:\n"
    "\n"
    "  1. PRESERVES verbatim every definitive user directive (anything the\n"
    "     user told the agent to do or not to do).\n"
    "  2. PRESERVES every actionable task item that is still pending.\n"
    "  3. PRESERVES architectural notes, file paths, function names, and any\n"
    "     concrete facts the agent has learned about the workspace.\n"
    "  4. HIGHLY SUMMARIZES the verbose operational history (tool call noise,\n"
    "     stack traces, search results, intermediate state).\n"
    "\n"
    "Return plain text. Do not invent facts; do not add tool calls."
)


def build_autocompact_prompt(history, system_prompt=None):
    """Compose the prompt sent to the secondary LLM call.

    Public so callers and tests can introspect what would be sent without
    actually invoking the model.
    """
    system = system_prompt or AUTOCOMPACT_SYSTEM_PROMPT
    lines = []
    for item in history:
        role = item.get("role", "?")
        if role == "tool":
            name = item.get("name", "?")
            lines.append(f"[tool:{name}] {item.get('content', '')}")
        else:
            lines.append(f"[{role}] {item.get('content', '')}")
    transcript = "\n".join(lines)
    return f"{system}\n\nTRANSCRIPT:\n{transcript}\n\nSUMMARY:\n"


def auto_compaction(
    history,
    *,
    model_client,
    max_new_tokens,
    preserve_recent,
    system_prompt=None,
):
    """Stage 5: invoke ``model_client`` with the ``autocompact`` prompt and
    fold its summary into the transcript in place of older items.

    The most recent ``preserve_recent`` items are kept verbatim so the agent
    can keep working from a fresh, accurate context. The summary is inserted
    as a synthetic ``assistant`` message at the start of the new history.

    ``model_client`` must expose a ``complete(prompt, max_new_tokens)`` method
    (the same protocol used by all model clients in this package).
    """
    cutoff = max(0, len(history) - preserve_recent)
    older = history[:cutoff]
    recent = history[cutoff:]
    if not older:
        return deepcopy(history)
    prompt = build_autocompact_prompt(older, system_prompt=system_prompt)
    summary = model_client.complete(prompt, max_new_tokens).strip()
    if not summary:
        summary = "[autocompact produced no summary]"
    synthetic = {
        "role": "assistant",
        "content": "[autocompact summary] " + summary,
        "compacted": True,
    }
    return [synthetic, *deepcopy(recent)]


# ---------------------------------------------------------------------------
# Cascade driver
# ---------------------------------------------------------------------------


def run_cascade(
    history,
    *,
    current_budget,
    config=None,
    model_client=None,
    autocompact_tokens=512,
    autocompact_prompt=None,
):
    """Run the graduated compaction cascade.

    Stages run in order; the cascade stops at the first stage that brings
    :func:`render_history_size` under ``target_chars``. Returns a dict with:

      * ``history``        - the (possibly transformed) history
      * ``budget``         - the recommended per-tool-output budget
      * ``stages_applied`` - list of stage names that ran
      * ``halted``         - True if the cascade hit a hard halt
    """
    cfg = {**DEFAULT_COMPACTION, **(config or {})}
    target = cfg["target_chars"]
    applied = []
    working = deepcopy(history)
    budget = current_budget

    if render_history_size(working) <= target:
        return {"history": working, "budget": budget, "stages_applied": applied, "halted": False}

    # Stage 1: budget reduction.
    budget = budget_reduction(
        working,
        current_budget=budget,
        target_chars=target,
        min_tool_output=cfg["min_tool_output"],
    )
    applied.append("budget_reduction")
    if render_history_size(working) <= target:
        return {"history": working, "budget": budget, "stages_applied": applied, "halted": False}

    # Stage 2: snipping.
    working = snipping(
        working,
        preserve_recent=cfg["preserve_recent"],
        fileread_tools=cfg["fileread_tools"],
        mcp_tools=cfg["mcp_tools"],
    )
    applied.append("snipping")
    if render_history_size(working) <= target:
        return {"history": working, "budget": budget, "stages_applied": applied, "halted": False}

    # Stage 3: microcompaction.
    working = microcompaction(
        working,
        preserve_recent=cfg["preserve_recent"],
        microcompact_clip=cfg["microcompact_clip"],
        fileread_tools=cfg["fileread_tools"],
        mcp_tools=cfg["mcp_tools"],
    )
    applied.append("microcompaction")
    if render_history_size(working) <= target:
        return {"history": working, "budget": budget, "stages_applied": applied, "halted": False}

    # Stage 4: context collapse.
    working = context_collapse(
        working,
        preserve_recent=cfg["preserve_recent"],
        fileread_tools=cfg["fileread_tools"],
        mcp_tools=cfg["mcp_tools"],
    )
    applied.append("context_collapse")
    if render_history_size(working) <= target:
        return {"history": working, "budget": budget, "stages_applied": applied, "halted": False}

    # Stage 5: auto-compaction (requires a model client).
    if model_client is not None:
        before = render_history_size(working)
        working = auto_compaction(
            working,
            model_client=model_client,
            max_new_tokens=autocompact_tokens,
            preserve_recent=cfg["preserve_recent"],
            system_prompt=autocompact_prompt,
        )
        applied.append("auto_compaction")
        after = render_history_size(working)
        if after <= target:
            return {"history": working, "budget": budget, "stages_applied": applied, "halted": False}
        # Hard halt: token thrashing detected.
        relief = (before - after) / max(before, 1)
        if relief < cfg["thrash_min_relief"]:
            raise HardHaltError(
                "compaction cascade exhausted: auto-compaction freed only "
                f"{relief * 100:.1f}% of the transcript (need >= "
                f"{cfg['thrash_min_relief'] * 100:.0f}%). Stopping to prevent "
                "continuous token thrashing."
            )

    return {"history": working, "budget": budget, "stages_applied": applied, "halted": True}
