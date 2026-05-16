"""Configuration loader.

The agent's prompts and harness parameters are loaded from a YAML file. The
defaults shipped with the package live in ``config/default.yaml``. Users can
override any subset of them via:

* ``--config PATH`` on the CLI
* ``.mini-coding-agent/config.yaml`` at the root of the workspace (auto-discovered)

PyYAML is an optional dependency. When PyYAML is not installed we fall back to
:data:`BUILTIN_DEFAULTS`, a hard-coded copy of ``default.yaml`` content. That
keeps the agent fully functional with just the standard library.
"""

from copy import deepcopy
from pathlib import Path


# A hard-coded mirror of ``config/default.yaml``. Used when PyYAML is not
# available so users never need to install YAML support just to run the agent.
# Keep this in sync with config/default.yaml. The test suite verifies that the
# Python dict and the YAML file describe the same defaults.
BUILTIN_DEFAULTS = {
    "prompts": {
        "agent_identity": (
            "You are Mini-Coding-Agent, a small, careful coding agent. You operate inside\n"
            "a real user workspace and complete tasks by calling structured tools. You\n"
            "plan briefly, take small steps, and prefer reading before writing.\n"
        ),
        "rules": [
            "Use tools instead of guessing about the workspace.",
            "Return exactly one <tool>...</tool> or one <final>...</final>.",
            'Tool calls must look like: <tool>{"name":"tool_name","args":{...}}</tool>',
            'For write_file and patch_file with multi-line text, prefer XML style: <tool name="write_file" path="file.py"><content>...</content></tool>',
            "Final answers must look like: <final>your answer</final>",
            "Never invent tool results.",
            "Keep answers concise and concrete.",
            "If the user asks you to create or update a specific file and the path is clear, use write_file or patch_file instead of repeatedly listing files.",
            "Before writing tests for existing code, read the implementation first.",
            "When writing tests, match the current implementation unless the user explicitly asked you to change the code.",
            "New files should be complete and runnable, including obvious imports.",
            "Do not repeat the same tool call with the same arguments if it did not help. Choose a different tool or return a final answer.",
            "Required tool arguments must not be empty. Do not call read_file, write_file, patch_file, run_shell, run_python, or delegate with args={}.",
            'After running a tool, always include the relevant output in your <final> answer. Never respond with just "Done." if there is actual output to show.',
        ],
        "examples": {
            "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
            "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
            "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
            "glob": '<tool>{"name":"glob","args":{"pattern":"**/*.py"}}</tool>',
            "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
            "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
            "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
            "run_python": '<tool>{"name":"run_python","args":{"code":"import sys; print(sys.version)","timeout":20}}</tool>',
            "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
        },
        "project_rules": "",
        "coordinator": (
            "You may delegate scoped sub-tasks to a read-only child agent via the\n"
            "`delegate` tool when:\n"
            "  - the sub-task is well-defined and read-only,\n"
            "  - the parent transcript is long enough that focused inspection would help,\n"
            "  - or the user explicitly asks for a separate investigation.\n"
            "Always include the result of the child in your final answer.\n"
        ),
        "override": "",
        "retry_notice": (
            "Runtime notice{problem_suffix}. Reply with a valid <tool> call or a non-empty <final> answer.\n"
            'For multi-line files, prefer <tool name="write_file" path="file.py"><content>...</content></tool>.'
        ),
        # System prompt used by stage-5 auto-compaction (see compaction.py).
        "autocompact": (
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
        ),
    },
    "harness": {
        "max_steps": 6,
        "max_new_tokens": 512,
        "max_depth": 1,
        "max_tool_output": 4000,
        "max_history": 12000,
        "temperature": 0.2,
        "top_p": 0.9,
        "ollama_timeout": 300,
        "openai_timeout": 60,
        "allowed_ops": None,
        "sandbox": "lite",
        "approval": "ask",
        # Graduated compaction cascade settings. See
        # :mod:`codelet.compaction` for the full semantics.
        "compaction": {
            "target_chars": 12000,
            "min_tool_output": 400,
            "microcompact_clip": 120,
            "preserve_recent": 4,
            "thrash_min_relief": 0.1,
            "mcp_tools": ["delegate"],
            "fileread_tools": ["read_file"],
            "auto_compaction": True,
            "autocompact_tokens": 2048,
        },
    },
    "project_rules_files": ["AGENTS.md", ".mini-coding-agent/rules.md"],
    # Hierarchical filesystem-backed memory (see codelet.memory_files).
    # Set ``enabled: false`` to disable; otherwise the agent scans well-known
    # CLAUDE.md / AGENTS.md / .claude/rules/*.md / CLAUDE.local.md locations
    # and appends up to ``max_files`` of them into the project-rules layer.
    "memory_files": {
        "enabled": True,
        "max_files": 5,
    },
    "sandbox": {},
}


def deep_merge(base, override):
    """Recursively merge ``override`` into a copy of ``base``."""
    result = deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _try_load_yaml(path):
    """Load a YAML file as a dict; raise RuntimeError if PyYAML is missing."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "Loading custom YAML config requires PyYAML. Install with: pip install pyyaml"
        ) from exc
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"config file must contain a YAML mapping: {path}")
    return data


def load_packaged_defaults():
    """Return the packaged defaults.

    Prefers ``config/default.yaml`` (when PyYAML is installed) so anyone
    editing that file sees their changes; falls back to ``BUILTIN_DEFAULTS``
    when PyYAML isn't available.
    """
    yaml_path = Path(__file__).parent / "config" / "default.yaml"
    try:
        return _try_load_yaml(yaml_path)
    except RuntimeError:
        # PyYAML missing - fall back to the Python copy.
        return deepcopy(BUILTIN_DEFAULTS)


def discover_workspace_config(repo_root):
    """Return the path to a workspace-level config override if present."""
    if not repo_root:
        return None
    candidate = Path(repo_root) / ".mini-coding-agent" / "config.yaml"
    return candidate if candidate.is_file() else None


def load_config(user_config_path=None, workspace_config_path=None):
    """Build the effective config by merging defaults < workspace < user.

    Parameters
    ----------
    user_config_path : str | Path | None
        Optional explicit override file (e.g. from ``--config``).
    workspace_config_path : str | Path | None
        Optional workspace-discovered override file.

    Returns
    -------
    dict
        Fully merged config dictionary.
    """
    config = load_packaged_defaults()
    if workspace_config_path:
        config = deep_merge(config, _try_load_yaml(workspace_config_path))
    if user_config_path:
        config = deep_merge(config, _try_load_yaml(user_config_path))
    return config


def load_project_rules(repo_root, rule_files):
    """Read project rule files relative to the repo root and concatenate them."""
    if not repo_root or not rule_files:
        return ""
    chunks = []
    seen = set()
    for name in rule_files:
        path = Path(repo_root) / name
        if not path.is_file():
            continue
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        chunks.append(f"# {name}\n{text}")
    return "\n\n".join(chunks)
