"""``.env`` file loader for the mini-coding-agent CLI.

The agent supports loading provider, model, key, and harness defaults from a
plain-text ``.env`` file at the workspace root (or any explicit path). This
keeps the CLI usable without exporting environment variables in every shell.

Supported keys (all optional; unset keys are ignored):

  LLM provider selection
  ----------------------
    LLM_PROVIDER       kimi | moonshot | zhipu | glm | siliconflow | openai |
                       deepseek | openrouter | together | dashscope | custom

  API keys (the matching one for the chosen provider wins)
  --------------------------------------------------------
    KIMI_API_KEY              MOONSHOT_API_KEY     (aliases)
    ZHIPU_API_KEY
    SILICONFLOW_API_KEY
    DEEPSEEK_API_KEY
    OPENROUTER_API_KEY
    TOGETHER_API_KEY
    DASHSCOPE_API_KEY
    OPENAI_API_KEY
    LLM_API_KEY               # generic override, beats provider-specific keys

  Model + endpoint (used for ``custom`` or to override the preset default)
  ------------------------------------------------------------------------
    LLM_MODEL
    LLM_BASE_URL

  Harness knobs (mapped onto the ``harness`` section of the YAML config)
  ---------------------------------------------------------------------
    MINI_AGENT_CMD              ignored by the agent itself (consumed by the
                                surrounding launcher); kept here for parity
                                with the documented .env schema.
    MINI_AGENT_MAX_STEPS        -> harness.max_steps
    MINI_AGENT_OPENAI_TIMEOUT   -> harness.openai_timeout
    MINI_AGENT_MAX_NEW_TOKENS   -> harness.max_new_tokens

The loader is implemented with the standard library only.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path


_PROVIDER_KEY_MAP = {
    "kimi": ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
    "moonshot": ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
    "glm": ("ZHIPU_API_KEY",),
    "zhipu": ("ZHIPU_API_KEY",),
    "siliconflow": ("SILICONFLOW_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "dashscope": ("DASHSCOPE_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "custom": ("CUSTOM_LLM_API_KEY",),
}


def parse_env_file(path):
    """Parse a ``.env`` file into a plain dict.

    Supports ``KEY=VALUE`` syntax with ``#`` line comments and optional
    surrounding quotes around the value. Lines that don't match are silently
    ignored, matching the behavior of common ``.env`` loaders.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    out = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        try:
            lexer = shlex.shlex(line, posix=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
            if not tokens:
                continue
            parsed_line = " ".join(tokens)
            if "=" not in parsed_line:
                continue
            key, _, value = parsed_line.partition("=")
            key = key.strip()
            if not key:
                continue
            out[key] = value.strip()
        except ValueError:
            continue
    return out


def discover_env_file(cwd=None):
    """Return the path to a ``.env`` file in the workspace, if present."""
    base = Path(cwd or ".").resolve()
    candidate = base / ".env"
    return candidate if candidate.is_file() else None


def load_env_into_environ(env, *, override=False):
    """Copy ``env`` into :data:`os.environ`.

    By default this is non-clobbering (an existing real environment variable
    wins). Set ``override=True`` to make ``.env`` values authoritative.
    """
    for key, value in env.items():
        if override or key not in os.environ:
            os.environ[key] = value


def resolve_api_key(provider, env):
    """Pick the right API key for ``provider`` out of a merged env dict.

    Resolution order:
      1. ``LLM_API_KEY``        - explicit generic override.
      2. provider-specific env vars (e.g. ``KIMI_API_KEY``).
      3. ``OPENAI_API_KEY``     - last-resort fallback.
    """
    if env.get("LLM_API_KEY"):
        return env["LLM_API_KEY"]
    candidates = _PROVIDER_KEY_MAP.get((provider or "").lower(), ())
    for name in candidates:
        if env.get(name):
            return env[name]
    return env.get("OPENAI_API_KEY")


def _coerce_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def env_to_overrides(env):
    """Translate a ``.env`` dict into structured agent overrides.

    Returns a dict with two top-level keys:

      ``cli``     - argparse-friendly attributes (provider, model,
                    openai_base_url, openai_api_key, openai_timeout,
                    max_steps, max_new_tokens). Only keys whose .env value is
                    set appear.
      ``harness`` - a partial harness config slice suitable for merging into
                    the YAML config under the ``harness`` key.

    The caller decides how aggressively to apply these (e.g. only when the
    user did *not* pass an explicit CLI flag).
    """
    cli = {}
    harness = {}

    provider = (env.get("LLM_PROVIDER") or "").strip().lower()
    if provider:
        cli["provider"] = provider

    if env.get("LLM_MODEL"):
        cli["model"] = env["LLM_MODEL"].strip()

    if env.get("LLM_BASE_URL"):
        cli["openai_base_url"] = env["LLM_BASE_URL"].strip()

    api_key = resolve_api_key(provider, env)
    if api_key:
        cli["openai_api_key"] = api_key

    max_steps = _coerce_int(env.get("MINI_AGENT_MAX_STEPS"))
    if max_steps is not None:
        cli["max_steps"] = max_steps
        harness["max_steps"] = max_steps

    timeout = _coerce_int(env.get("MINI_AGENT_OPENAI_TIMEOUT"))
    if timeout is not None:
        cli["openai_timeout"] = timeout
        harness["openai_timeout"] = timeout

    max_tokens = _coerce_int(env.get("MINI_AGENT_MAX_NEW_TOKENS"))
    if max_tokens is not None:
        cli["max_new_tokens"] = max_tokens
        harness["max_new_tokens"] = max_tokens

    tool_timeout = _coerce_int(env.get("MINI_AGENT_TOOL_TIMEOUT"))
    if tool_timeout is not None:
        harness["tool_timeout"] = tool_timeout

    tool_max_timeout = _coerce_int(env.get("MINI_AGENT_TOOL_MAX_TIMEOUT"))
    if tool_max_timeout is not None:
        harness["tool_max_timeout"] = tool_max_timeout

    return {"cli": cli, "harness": harness}


def load_env_config(path=None, cwd=None):
    """High-level entry point: discover ``.env``, parse it, and return overrides.

    Parameters
    ----------
    path : str | Path | None
        Explicit ``.env`` path. When ``None`` the loader looks for
        ``<cwd>/.env``.
    cwd : str | Path | None
        Workspace root used for discovery (default: process cwd).

    Returns ``(env_dict, overrides_dict)``. Both are empty when no file was
    found, so the caller does not need to guard against ``None``.
    """
    if path is None:
        path = discover_env_file(cwd)
    if not path:
        return {}, {"cli": {}, "harness": {}}
    env = parse_env_file(path)
    return env, env_to_overrides(env)
