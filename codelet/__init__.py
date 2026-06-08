"""Mini-Coding-Agent: a small, configurable coding agent.

This package replaces the original single-file ``codelet.py``. The
public API (everything historically importable from ``codelet``) is
re-exported here so existing scripts and tests keep working unchanged.

Module map:

* :mod:`codelet.utils`     - small helpers and constants.
* :mod:`codelet.sandbox`   - sandboxing policy for risky tools.
* :mod:`codelet.providers` - OpenAI-compatible provider presets.
* :mod:`codelet.clients`   - model client implementations.
* :mod:`codelet.parsing`   - parsing of model output.
* :mod:`codelet.workspace` - WorkspaceContext snapshot.
* :mod:`codelet.sessions`  - SessionStore on-disk persistence.
* :mod:`codelet.tools`     - tool registry + implementations.
* :mod:`codelet.prompt`    - six-layer XML prompt assembly.
* :mod:`codelet.config`    - YAML-backed configuration loader.
* :mod:`codelet.agent`     - the MiniAgent orchestrator.
* :mod:`codelet.welcome`   - ASCII welcome banner.
* :mod:`codelet.cli`       - argparse CLI and ``main`` entry point.
"""

from .agent import MiniAgent
from .baseline import (
    capture_baseline,
    diff_baseline,
    verify_session_baseline,
)
from .cli import build_agent, build_arg_parser, main, _post_process_args
from .clients import FakeModelClient, OllamaModelClient, OpenAIModelClient
from .compaction import (
    AUTOCOMPACT_SYSTEM_PROMPT,
    CHECKPOINT_MARKER,
    CHECKPOINT_SYSTEM_PROMPT,
    DEFAULT_COMPACTION,
    HardHaltError,
    apply_tool_output_budget,
    auto_compaction,
    budget_reduction,
    build_autocompact_prompt,
    checkpoint_summary,
    context_collapse,
    has_checkpoint,
    microcompaction,
    render_history_size,
    run_cascade,
    snipping,
)
from .config import (
    BUILTIN_DEFAULTS,
    deep_merge,
    discover_workspace_config,
    load_config,
    load_packaged_defaults,
    load_project_rules,
)
from .env_config import (
    discover_env_file,
    env_to_overrides,
    load_env_config,
    load_env_into_environ,
    parse_env_file,
    resolve_api_key,
)
from .memory_files import (
    DEFAULT_MAX_FILES,
    ENTRYPOINT_NAME,
    LAYER_WEIGHTS,
    MAX_SCAN_FILES,
    MEMORY_TYPES,
    MemoryHeader,
    discover_memory_files,
    ensure_memory_dir_exists,
    format_memory_manifest,
    is_auto_memory_enabled,
    memory_age_days,
    memory_freshness_text,
    render_memory_files,
    scan_memory_headers,
    select_memory_files,
    truncate_entrypoint_content,
    validate_memory_path,
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
from .stop_reason import AskResult, StopReason
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
    clip_head_tail,
    dedupe_lines,
    middle,
    now,
    strip_ansi,
)
from .welcome import build_welcome
from .workspace import WorkspaceContext


__all__ = [
    "ALL_TOOL_OPS",
    "AUTOCOMPACT_SYSTEM_PROMPT",
    "AskResult",
    "BUILTIN_DEFAULTS",
    "CHECKPOINT_MARKER",
    "CHECKPOINT_SYSTEM_PROMPT",
    "DEFAULT_COMPACTION",
    "DEFAULT_MAX_FILES",
    "DOC_NAMES",
    "FakeModelClient",
    "HELP_DETAILS",
    "HELP_TEXT",
    "HardHaltError",
    "IGNORED_PATH_NAMES",
    "LAYER_WEIGHTS",
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
    "StopReason",
    "ToolRegistry",
    "WELCOME_ART",
    "WorkspaceContext",
    "_post_process_args",
    "auto_compaction",
    "budget_reduction",
    "build_agent",
    "build_arg_parser",
    "build_autocompact_prompt",
    "build_history_text",
    "build_memory_text",
    "build_prefix",
    "build_prompt",
    "build_welcome",
    "capture_baseline",
    "checkpoint_summary",
    "clip",
    "clip_head_tail",
    "context_collapse",
    "dedupe_lines",
    "deep_merge",
    "diff_baseline",
    "discover_env_file",
    "discover_memory_files",
    "discover_workspace_config",
    "env_to_overrides",
    "extract",
    "extract_raw",
    "has_checkpoint",
    "load_config",
    "load_env_config",
    "load_env_into_environ",
    "load_packaged_defaults",
    "load_project_rules",
    "main",
    "microcompaction",
    "middle",
    "now",
    "parse_attrs",
    "parse_env_file",
    "parse_model_output",
    "parse_xml_tool",
    "render_history_size",
    "render_memory_files",
    "resolve_api_key",
    "resolve_provider_preset",
    "retry_notice",
    "run_cascade",
    "sandbox_check_python",
    "sandbox_check_shell",
    "sandbox_filter_env",
    "sandbox_preexec",
    "select_memory_files",
    "snipping",
    "strip_ansi",
    "verify_session_baseline",
]
