"""Command-line interface entry point."""

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style

from .agent import MiniAgent
from .clients import OllamaModelClient, OpenAIModelClient
from .config import deep_merge, discover_workspace_config, load_config
from .env_config import load_env_config, load_env_into_environ
from .providers import LLM_PROVIDER_PRESETS, resolve_provider_preset
from .sessions import SessionStore
from .utils import HELP_DETAILS
from .welcome import build_welcome
from .workspace import WorkspaceContext

console = Console()

_SLASH_COMMANDS = ["/exit", "/quit", "/help", "/memory", "/session", "/reset"]


def _make_tool_output_callback():
    """Return a callback that prints tool output in a styled panel after each tool call."""
    def callback(name, args, result):
        panel = Panel(
            str(result).strip(),
            title=f"🔧 Tool Execution: {name}",
            style="dim",
            border_style="black",
        )
        console.print(panel)
    return callback


def _make_machine_tool_callback():
    """Return a callback that prints raw XML tool blocks for machine-readable output."""
    import json as _json

    def callback(name, args, result):
        sys.stdout.write(f'<tool name="{name}">{_json.dumps(args)}</tool>\n')
        sys.stdout.flush()
    return callback


def build_agent(args):
    """Construct a MiniAgent from parsed CLI arguments."""
    machine_mode = getattr(args, "machine", False)
    workspace = WorkspaceContext.build(args.cwd)
    store = SessionStore(Path(workspace.repo_root) / ".codelet" / "sessions")

    # Discover and apply .env. Env-vars set by .env feed both the resolution
    # of provider keys later in this function (via os.environ) and the
    # ``harness`` config slice via env_to_overrides. An explicit --env-file
    # CLI flag overrides the auto-discovered ``.env`` at the workspace root.
    # Explicit --env-file wins; otherwise discover from the workspace root.
    # We always pass cwd=workspace.repo_root so discovery uses the project
    # directory, NOT the process CWD (which would be the pytest runner dir
    # during tests and could accidentally load an unrelated .env).
    env_file = getattr(args, "env_file", None)
    env_dict, env_overrides = load_env_config(path=env_file, cwd=workspace.repo_root)
    if env_dict:
        load_env_into_environ(env_dict, override=False)

    # Merge YAML defaults < workspace override < explicit --config.
    workspace_cfg = discover_workspace_config(workspace.repo_root)
    config = load_config(
        user_config_path=getattr(args, "config", None),
        workspace_config_path=workspace_cfg,
    )
    # .env-driven harness overrides apply after YAML so users can pin steps /
    # timeouts / tokens from the file. CLI flags still win (see below).
    if env_overrides.get("harness"):
        config = deep_merge(config, {"harness": env_overrides["harness"]})

    # .env-driven CLI defaults: only fill in when the user did NOT pass the
    # corresponding CLI flag. This preserves the documented precedence
    # (CLI > .env > YAML > built-in defaults).
    env_cli = env_overrides.get("cli", {})
    if env_cli.get("provider") and not getattr(args, "provider", None):
        args.provider = env_cli["provider"]
    if env_cli.get("model") and not getattr(args, "model_explicit", False):
        args.model = env_cli["model"]
        args.model_explicit = True
    if env_cli.get("openai_base_url") and not getattr(args, "openai_base_url_explicit", False):
        args.openai_base_url = env_cli["openai_base_url"]
        args.openai_base_url_explicit = True
    if env_cli.get("openai_api_key") and not args.openai_api_key:
        args.openai_api_key = env_cli["openai_api_key"]
    if env_cli.get("max_steps") is not None and not getattr(args, "_max_steps_explicit", False):
        args.max_steps = env_cli["max_steps"]
    if env_cli.get("max_new_tokens") is not None and not getattr(args, "_max_new_tokens_explicit", False):
        args.max_new_tokens = env_cli["max_new_tokens"]
    if env_cli.get("openai_timeout") is not None and not getattr(args, "_openai_timeout_explicit", False):
        args.openai_timeout = env_cli["openai_timeout"]

    harness_cfg = config.get("harness", {})

    # Apply harness defaults from config when the user did not explicitly pass
    # a flag. We can detect "not explicit" by reading argparse SUPPRESS-style
    # attrs only when present.
    if not getattr(args, "_max_steps_explicit", False):
        args.max_steps = harness_cfg.get("max_steps", args.max_steps)
    if not getattr(args, "_max_new_tokens_explicit", False):
        args.max_new_tokens = harness_cfg.get("max_new_tokens", args.max_new_tokens)
    if not getattr(args, "_temperature_explicit", False):
        args.temperature = harness_cfg.get("temperature", args.temperature)
    if not getattr(args, "_top_p_explicit", False):
        args.top_p = harness_cfg.get("top_p", args.top_p)
    if not getattr(args, "_ollama_timeout_explicit", False):
        args.ollama_timeout = harness_cfg.get("ollama_timeout", args.ollama_timeout)
    if not getattr(args, "_openai_timeout_explicit", False):
        args.openai_timeout = harness_cfg.get("openai_timeout", args.openai_timeout)
    if args.approval is None:
        # Config-supplied approval policy is a fallback only; the main()
        # caller may set it to "auto" earlier for one-shot prompts.
        args.approval = harness_cfg.get("approval", "ask")
    if args.allow is None and harness_cfg.get("allowed_ops"):
        args.allow = list(harness_cfg["allowed_ops"])
    if not getattr(args, "_sandbox_explicit", False) and harness_cfg.get("sandbox"):
        args.sandbox = harness_cfg["sandbox"]

    # Phase-6 hardening flags: CLI > config. We mutate the config in place so
    # MiniAgent's __init__ picks them up uniformly through harness.* settings.
    if getattr(args, "decoy_tools", False):
        harness_cfg["decoy_tools"] = True
        config["harness"] = harness_cfg
    if getattr(args, "yolo", False):
        harness_cfg["yolo_classifier"] = True
        config["harness"] = harness_cfg
    if getattr(args, "undercover", False):
        os.environ["CODELET_UNDERCOVER"] = "1"

    preset = resolve_provider_preset(args.provider) if getattr(args, "provider", None) else None
    if preset is not None:
        args.backend = "openai"
        if not args.openai_base_url_explicit and preset.get("base_url"):
            args.openai_base_url = preset["base_url"]
        if not args.openai_api_key:
            args.openai_api_key = os.environ.get(preset["env_key"]) or os.environ.get("OPENAI_API_KEY")
        if not args.model_explicit and preset.get("default_model"):
            args.model = preset["default_model"]

    if args.backend == "openai":
        api_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            hint = ""
            if preset is not None:
                hint = f" (e.g. export {preset['env_key']}=...)"
            raise RuntimeError(
                "OpenAI-compatible API key is required."
                f" Set --openai-api-key or the OPENAI_API_KEY environment variable{hint}."
            )
        model_client = OpenAIModelClient(
            model=args.model,
            api_key=api_key,
            base_url=args.openai_base_url,
            temperature=args.temperature,
            top_p=args.top_p,
            timeout=args.openai_timeout,
        )
    else:
        model_client = OllamaModelClient(
            model=args.model,
            host=args.host,
            temperature=args.temperature,
            top_p=args.top_p,
            timeout=args.ollama_timeout,
        )

    allowed_ops = set(args.allow) if args.allow else None

    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    tool_cb = _make_machine_tool_callback() if machine_mode else _make_tool_output_callback()
    if session_id:
        return MiniAgent.from_session(
            model_client=model_client,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            allowed_ops=allowed_ops,
            sandbox=args.sandbox,
            config=config,
            tool_output_callback=tool_cb,
        )
    return MiniAgent(
        model_client=model_client,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        allowed_ops=allowed_ops,
        sandbox=args.sandbox,
        config=config,
        tool_output_callback=tool_cb,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent supporting Ollama and OpenAI-compatible backends.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot task prompt (runs non-interactively with auto-approval by default).")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a YAML config file that overrides packaged defaults (prompts and harness settings).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help=(
            "Path to a .env file with LLM provider/key/model and harness "
            "overrides. When omitted, the agent auto-discovers .env at the "
            "workspace root. Supported keys include LLM_PROVIDER, LLM_API_KEY, "
            "LLM_MODEL, LLM_BASE_URL, KIMI_API_KEY/MOONSHOT_API_KEY, "
            "ZHIPU_API_KEY, SILICONFLOW_API_KEY, OPENAI_API_KEY, "
            "MINI_AGENT_MAX_STEPS, MINI_AGENT_OPENAI_TIMEOUT, "
            "MINI_AGENT_MAX_NEW_TOKENS."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=("ollama", "openai"),
        default="ollama",
        help="Model backend to use.",
    )
    provider_choices = sorted(LLM_PROVIDER_PRESETS.keys())
    parser.add_argument(
        "--provider",
        choices=provider_choices,
        default=None,
        help=(
            "Convenience preset for a custom OpenAI-compatible LLM API. "
            "Sets --backend openai and an appropriate --openai-base-url, and reads "
            "the API key from the provider's conventional environment variable "
            "(e.g. MOONSHOT_API_KEY for kimi, ZHIPU_API_KEY for glm, "
            "SILICONFLOW_API_KEY for siliconflow). Pick 'custom' to combine with "
            "--openai-base-url and --openai-api-key for any other endpoint."
        ),
    )
    parser.add_argument("--model", default=argparse.SUPPRESS, help="Model name (Ollama model or OpenAI model id). Default: qwen3.5:4b, or provider preset default.")
    parser.add_argument("--host", default="http://127.0.0.1:11434", help="Ollama server URL.")
    parser.add_argument("--ollama-timeout", type=int, default=argparse.SUPPRESS, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-api-key", default=None, help="OpenAI API key (falls back to OPENAI_API_KEY env var, or the preset-specific env var when --provider is set).")
    parser.add_argument("--openai-base-url", default=argparse.SUPPRESS, help="Base URL for OpenAI-compatible API. Default: https://api.openai.com/v1, or provider preset value.")
    parser.add_argument("--openai-timeout", type=int, default=argparse.SUPPRESS, help="OpenAI request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default=None,
        help="Approval policy for risky tools. Defaults to 'auto' when a task prompt is given (delegation mode), 'ask' for interactive mode.",
    )
    parser.add_argument(
        "--allow",
        nargs="+",
        choices=("read", "write", "bash", "python"),
        default=None,
        metavar="OP",
        help="Allowed tool categories: read, write, bash, python. Defaults to all when not specified.",
    )
    parser.add_argument(
        "--sandbox",
        choices=("off", "lite"),
        default=argparse.SUPPRESS,
        help=(
            "Lightweight sandboxing for risky tools (run_shell, run_python). "
            "'lite' (default) blocks obviously destructive command patterns, "
            "strips sensitive environment variables from subprocesses, and applies "
            "POSIX resource limits (CPU, memory, file size, processes). 'off' "
            "disables all of the above."
        ),
    )
    parser.add_argument("--max-steps", type=int, default=argparse.SUPPRESS, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=argparse.SUPPRESS, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=argparse.SUPPRESS, help="Sampling temperature.")
    parser.add_argument("--top-p", type=float, default=argparse.SUPPRESS, help="Top-p nucleus sampling value.")
    parser.add_argument("--no-welcome", action="store_true", default=False, help="Suppress the welcome banner at startup.")
    parser.add_argument("--machine", action="store_true", default=False,
                        help="Machine-readable raw XML output without UI formatting. Disables spinners and rich markdown; streams raw <tool> and <final> blocks for programmatic consumers.")
    parser.add_argument("--decoy-tools", action="store_true", default=False,
                        help="Inject decoy tool entries into the prompt (anti-distillation; calls are refused).")
    parser.add_argument("--yolo", action="store_true", default=False,
                        help="Auto-approve obviously-safe shell commands (ls/pwd/cat/git status/...) when approval=ask.")
    parser.add_argument("--undercover", action="store_true", default=False,
                        help="Use a generic helpful-assistant identity and suppress the welcome banner. Equivalent to MINI_AGENT_UNDERCOVER=1.")
    return parser


def _post_process_args(args):
    """Apply defaults; mark explicit-vs-config-driven attrs for build_agent."""
    args.model_explicit = hasattr(args, "model")
    if not args.model_explicit:
        args.model = "qwen3.5:4b"
    args.openai_base_url_explicit = hasattr(args, "openai_base_url")
    if not args.openai_base_url_explicit:
        args.openai_base_url = "https://api.openai.com/v1"

    # Mark each harness flag as explicit if the user passed it, then fill in
    # the original CLI-level default so legacy callers see the same behavior.
    args._max_steps_explicit = hasattr(args, "max_steps")
    if not args._max_steps_explicit:
        args.max_steps = 6
    args._max_new_tokens_explicit = hasattr(args, "max_new_tokens")
    if not args._max_new_tokens_explicit:
        args.max_new_tokens = 512
    args._temperature_explicit = hasattr(args, "temperature")
    if not args._temperature_explicit:
        args.temperature = 0.2
    args._top_p_explicit = hasattr(args, "top_p")
    if not args._top_p_explicit:
        args.top_p = 0.9
    args._ollama_timeout_explicit = hasattr(args, "ollama_timeout")
    if not args._ollama_timeout_explicit:
        args.ollama_timeout = 300
    args._openai_timeout_explicit = hasattr(args, "openai_timeout")
    if not args._openai_timeout_explicit:
        args.openai_timeout = 60
    args._sandbox_explicit = hasattr(args, "sandbox")
    if not args._sandbox_explicit:
        args.sandbox = "lite"
    return args


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    # Resolve approval before _post_process_args so the harness-config fallback
    # inside that function (which always writes "ask") doesn't shadow the
    # auto-approval logic for one-shot prompts.
    if args.approval is None:
        args.approval = "auto" if args.prompt else "ask"
    args = _post_process_args(args)

    # Machine mode: force line-buffered stdout so cowork's readline() loop
    # receives output immediately rather than waiting for a full buffer flush.
    if getattr(args, "machine", False):
        sys.stdout.reconfigure(line_buffering=True)

    agent = build_agent(args)

    backend_label = args.backend
    if getattr(args, "provider", None):
        backend_label = f"{args.backend} ({args.provider})"
    if not args.no_welcome and not getattr(args, "undercover", False) and not getattr(args, "machine", False):
        welcome_text = build_welcome(agent, model=args.model, backend=backend_label)
        console.print(Panel(welcome_text, title="🚀 Codelet Agent", border_style="blue"))

    if args.prompt:
        prompt = " ".join(args.prompt).strip()
        if prompt:
            if getattr(args, "machine", False):
                # RAW MODE: no spinners, no markdown; stream <tool> blocks via
                # _make_machine_tool_callback and wrap the final answer in <final>.
                try:
                    response = agent.ask(prompt)
                    sys.stdout.write(f"<final>{response}</final>\n")
                    sys.stdout.flush()
                except RuntimeError as exc:
                    sys.stderr.write(str(exc) + "\n")
                    return 1
            else:
                console.print()
                try:
                    with console.status("[bold green]Thinking...", spinner="dots"):
                        response = agent.ask(prompt)
                    console.print(Markdown(response))
                except RuntimeError as exc:
                    console.print(str(exc), style="bold red")
                    return 1
        return 0

    # Set up prompt_toolkit session with slash-command autocomplete.
    _command_completer = WordCompleter(_SLASH_COMMANDS, ignore_case=True)
    _prompt_style = Style.from_dict({"prompt": "ansicyan bold"})
    # Fall back to plain input() when stdin is not a tty (e.g. CI/piped input).
    use_prompt_toolkit = sys.stdin.isatty()
    if use_prompt_toolkit:
        prompt_session = PromptSession(completer=_command_completer, style=_prompt_style)

    while True:
        try:
            if use_prompt_toolkit:
                user_input = prompt_session.prompt("\ncodelet> ").strip()
            else:
                user_input = input("\ncodelet> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("")
            return 0

        if not user_input:
            continue
        if user_input in {"/exit", "/quit"}:
            return 0
        if user_input == "/help":
            console.print(Panel(Markdown(HELP_DETAILS), title="Help", border_style="cyan"))
            continue
        if user_input == "/memory":
            memory = agent.session["memory"]
            table = Table(title="Agent Working Memory", show_lines=True)
            table.add_column("Key", style="cyan", no_wrap=True)
            table.add_column("Value")
            table.add_row("Task", memory.get("task") or "-")
            table.add_row("Files", ", ".join(memory.get("files", [])) or "-")
            notes = memory.get("notes", [])
            table.add_row("Notes", "\n".join(f"• {n}" for n in notes) if notes else "-")
            console.print(table)
            continue
        if user_input == "/session":
            console.print(str(agent.session_path))
            continue
        if user_input == "/reset":
            agent.reset()
            console.print("[bold green]Session reset.[/bold green]")
            continue

        console.print()
        try:
            with console.status("[bold green]Thinking...", spinner="dots"):
                response = agent.ask(user_input)
            console.print(Markdown(response))
        except RuntimeError as exc:
            console.print(str(exc), style="bold red")
