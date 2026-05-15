"""Mini-Coding-Agent: a small, configurable coding agent.

This package replaces the original single-file ``mini_coding_agent.py``. The
public API (everything historically importable from ``mini_coding_agent``) is
re-exported here so existing scripts and tests keep working unchanged.

Module map:

* :mod:`mini_coding_agent.utils`     - small helpers and constants.
* :mod:`mini_coding_agent.sandbox`   - sandboxing policy for risky tools.
* :mod:`mini_coding_agent.providers` - OpenAI-compatible provider presets.
* :mod:`mini_coding_agent.clients`   - model client implementations.
* :mod:`mini_coding_agent.parsing`   - parsing of model output.
* :mod:`mini_coding_agent.workspace` - WorkspaceContext snapshot.
* :mod:`mini_coding_agent.sessions`  - SessionStore on-disk persistence.
* :mod:`mini_coding_agent.tools`     - tool registry + implementations.
* :mod:`mini_coding_agent.prompt`    - six-layer XML prompt assembly.
* :mod:`mini_coding_agent.config`    - YAML-backed configuration loader.
* :mod:`mini_coding_agent.agent`     - the MiniAgent orchestrator.
* :mod:`mini_coding_agent.welcome`   - ASCII welcome banner.
* :mod:`mini_coding_agent.cli`       - argparse CLI and ``main`` entry point.
"""

from .agent import MiniAgent
from .cli import build_agent, build_arg_parser, main, _post_process_args
from .clients import FakeModelClient, OllamaModelClient, OpenAIModelClient
from .config import (
    BUILTIN_DEFAULTS,
    deep_merge,
    discover_workspace_config,
    load_config,
    load_packaged_defaults,
    load_project_rules,
)
from .parsing import (
    extract,
    extract_raw,
    parse_attrs,
    parse_model_output,
    parse_xml_tool,
    retry_notice,
)
from .prompt import (
    build_history_text,
    build_memory_text,
    build_prefix,
    build_prompt,
)
from .providers import LLM_PROVIDER_PRESETS, resolve_provider_preset
from .sandbox import (
    DEFAULT_ENV_STRIP_PATTERNS,
    DEFAULT_LIMITS,
    DEFAULT_PYTHON_DENY_PATTERNS,
    DEFAULT_SHELL_DENY_PATTERNS,
    SANDBOX_DEFAULT_LIMITS,
    SANDBOX_ENV_STRIP_PATTERNS,
    SANDBOX_PYTHON_DENY_PATTERNS,
    SANDBOX_SHELL_DENY_PATTERNS,
    sandbox_check_python,
    sandbox_check_shell,
    sandbox_filter_env,
    sandbox_preexec,
)
from .sessions import SessionStore
from .tools import ToolRegistry
from .utils import (
    ALL_TOOL_OPS,
    DOC_NAMES,
    HELP_DETAILS,
    HELP_TEXT,
    IGNORED_PATH_NAMES,
    MAX_HISTORY,
    MAX_TOOL_OUTPUT,
    WELCOME_ART,
    clip,
    middle,
    now,
)
from .welcome import build_welcome
from .workspace import WorkspaceContext


__all__ = [
    "ALL_TOOL_OPS",
    "BUILTIN_DEFAULTS",
    "DOC_NAMES",
    "FakeModelClient",
    "HELP_DETAILS",
    "HELP_TEXT",
    "IGNORED_PATH_NAMES",
    "LLM_PROVIDER_PRESETS",
    "MAX_HISTORY",
    "MAX_TOOL_OUTPUT",
    "MiniAgent",
    "OllamaModelClient",
    "OpenAIModelClient",
    "SANDBOX_DEFAULT_LIMITS",
    "SANDBOX_ENV_STRIP_PATTERNS",
    "SANDBOX_PYTHON_DENY_PATTERNS",
    "SANDBOX_SHELL_DENY_PATTERNS",
    "SessionStore",
    "ToolRegistry",
    "WELCOME_ART",
    "WorkspaceContext",
    "_post_process_args",
    "build_agent",
    "build_arg_parser",
    "build_history_text",
    "build_memory_text",
    "build_prefix",
    "build_prompt",
    "build_welcome",
    "clip",
    "deep_merge",
    "discover_workspace_config",
    "extract",
    "extract_raw",
    "load_config",
    "load_packaged_defaults",
    "load_project_rules",
    "main",
    "middle",
    "now",
    "parse_attrs",
    "parse_model_output",
    "parse_xml_tool",
    "resolve_provider_preset",
    "retry_notice",
    "sandbox_check_python",
    "sandbox_check_shell",
    "sandbox_filter_env",
    "sandbox_preexec",
]
