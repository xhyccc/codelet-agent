"""Prompt assembly using a six-layer, XML-tagged architecture.

Inspired by Claude Code's harness, the prompt is split into six layers ordered
from most stable to most volatile so the upper portion can be reused by KV
caches across turns. Layers are wrapped in XML tags rather than markdown
headers so they are semantically isolated and unambiguous to parse.

Layer order (top -> bottom = most stable -> most volatile):

1. ``<agent-identity>``   - who the agent is.
2. ``<system-defaults>``  - immutable rules, available tools, syntax examples.
3. ``<project-rules>``    - per-repo overrides (AGENTS.md / config).
4. ``<coordinator>``      - delegation/swarm guidance (only when delegation is enabled).
5. ``<override>``         - session overrides supplied via config or CLI.
6. ``<workspace>``        - workspace snapshot (cwd, branch, status, docs).

The truly volatile turn-state - ``<memory>``, ``<transcript>``, ``<request>``
- is appended on every call to :func:`build_prompt` so the cached prefix
stays stable.

Existing flush-left labels (``Rules:``, ``Tools:``, ``Valid response examples:``,
``Workspace:``, ``Memory:``, ``Transcript:``, ``Current user request:``) are
preserved inside their respective tags for human readability and downstream
log scraping.
"""

from .utils import clip


def _wrap(tag, body):
    """Wrap a non-empty body in an XML tag; return ``""`` if body is empty."""
    body = (body or "").strip("\n")
    if not body:
        return ""
    return f"<{tag}>\n{body}\n</{tag}>"


def render_tools_block(tools):
    """Render the available tools and example payloads as a single text block."""
    tool_lines = []
    for name, tool in tools.items():
        fields = ", ".join(f"{key}: {value}" for key, value in tool["schema"].items())
        risk = "approval required" if tool["risky"] else "safe"
        tool_lines.append(f"- {name}({fields}) [{risk}] {tool['description']}")
    return "\n".join(tool_lines)


def render_examples_block(tools, examples_map):
    """Render the response-example block, filtered to the active tool set."""
    example_lines = [examples_map[name] for name in tools if name in examples_map]
    example_lines.append("<final>Done.</final>")
    return "\n".join(example_lines)


def render_rules_block(rules):
    """Render the bullet list of rules into a single text block."""
    return "\n".join(f"- {rule}" for rule in rules)


def build_prefix(prompts_cfg, tools, workspace_text, project_rules_text=""):
    """Assemble the stable prefix layers in cache-friendly order.

    Parameters
    ----------
    prompts_cfg : dict
        The ``prompts`` section of the loaded config.
    tools : dict
        Mapping of tool name -> tool spec (used to filter examples and
        decide whether to emit the ``<coordinator>`` layer).
    workspace_text : str
        Output of :meth:`WorkspaceContext.text` for the active workspace.
    project_rules_text : str
        Optional pre-rendered project-specific rules (e.g. AGENTS.md).
    """
    rules_block = render_rules_block(prompts_cfg.get("rules") or [])
    tools_block = render_tools_block(tools)
    examples_block = render_examples_block(tools, prompts_cfg.get("examples") or {})

    system_defaults = "\n\n".join([
        "Rules:\n" + rules_block,
        "Tools:\n" + tools_block,
        "Valid response examples:\n" + examples_block,
    ])

    # Compose the project-rules layer: configured text + any text loaded from
    # AGENTS.md-style files in the workspace.
    configured_rules = (prompts_cfg.get("project_rules") or "").strip()
    project_rules_layer = "\n\n".join(part for part in (configured_rules, project_rules_text.strip()) if part)

    coordinator_layer = ""
    if "delegate" in tools:
        coordinator_layer = (prompts_cfg.get("coordinator") or "").strip()

    override_layer = (prompts_cfg.get("override") or "").strip()

    layers = [
        _wrap("agent-identity", (prompts_cfg.get("agent_identity") or "").strip()),
        _wrap("system-defaults", system_defaults),
        _wrap("project-rules", project_rules_layer),
        _wrap("coordinator", coordinator_layer),
        _wrap("override", override_layer),
        _wrap("workspace", workspace_text),
    ]
    return "\n\n".join(layer for layer in layers if layer)


def build_memory_text(memory):
    """Format the working-memory block (kept flush-left under ``Memory:``)."""
    notes = "\n".join(f"- {note}" for note in memory["notes"]) or "- none"
    return "\n".join([
        "Memory:",
        f"- task: {memory['task'] or '-'}",
        f"- files: {', '.join(memory['files']) or '-'}",
        "- notes:",
        notes,
    ])


def build_history_text(history, *, max_tool_output, max_history):
    """Render the rolling transcript, with read deduplication and clipping."""
    if not history:
        return "- empty"

    import json

    lines = []
    seen_reads = set()
    recent_start = max(0, len(history) - 6)
    for index, item in enumerate(history):
        recent = index >= recent_start
        if item["role"] == "tool" and item["name"] in ("write_file", "patch_file"):
            path = str(item["args"].get("path", ""))
            seen_reads.discard(path)
        if item["role"] == "tool" and item["name"] == "read_file" and not recent:
            path = str(item["args"].get("path", ""))
            if path in seen_reads:
                continue
            seen_reads.add(path)

        if item["role"] == "tool":
            limit = 900 if recent else 180
            lines.append(f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True)}")
            lines.append(clip(item["content"], limit))
        else:
            limit = 900 if recent else 220
            lines.append(f"[{item['role']}] {clip(item['content'], limit)}")

    return clip("\n".join(lines), max_history)


def build_prompt(prefix, memory_text, history_text, user_message):
    """Compose the full prompt: stable prefix + volatile turn state.

    The volatile blocks are wrapped in ``<memory>``, ``<transcript>``, and
    ``<request>`` so the model can use them as semantically distinct sources.
    """
    return "\n\n".join([
        prefix,
        _wrap("memory", memory_text),
        _wrap("transcript", "Transcript:\n" + history_text),
        _wrap("request", "Current user request:\n" + user_message),
    ])
