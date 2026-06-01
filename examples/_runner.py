"""Shared helpers for the codelet examples.

These examples run the **real** agent against the OpenAI-compatible endpoint
configured in your workspace ``.env`` file (``LLM_PROVIDER`` / ``LLM_BASE_URL``
/ ``LLM_MODEL`` / ``LLM_API_KEY``).  Secrets are read straight from ``.env`` and
are never printed.

Usage from an example script::

    from _runner import build_agent_from_env

    agent = build_agent_from_env(workdir)
    print(agent.ask("List the files in this directory."))
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repository root (which contains the ``codelet`` package) is on the
# import path when this file is run directly as ``python examples/...``.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from codelet import MiniAgent, OpenAIModelClient, SessionStore, WorkspaceContext
from codelet.env_config import load_env_config


def build_model_client_from_env():
    """Construct an ``OpenAIModelClient`` from the workspace ``.env`` file.

    Resolves provider/base-url/model/api-key exactly the way the CLI does. The
    API key is taken from ``.env`` (or a real environment variable) and is
    never logged.
    """
    env, overrides = load_env_config(cwd=str(REPO_ROOT))
    cli = overrides.get("cli", {})

    model = cli.get("model") or env.get("LLM_MODEL") or "gpt-4o-mini"
    base_url = cli.get("openai_base_url") or env.get("LLM_BASE_URL")
    api_key = cli.get("openai_api_key") or os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "No API key found. Add LLM_API_KEY (or OPENAI_API_KEY) to your "
            ".env file at the repository root."
        )

    return OpenAIModelClient(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.2,
        top_p=0.95,
        timeout=120,
    )


def build_agent_from_env(workdir, **kwargs):
    """Build a ready-to-run :class:`MiniAgent` rooted at ``workdir``.

    ``kwargs`` are forwarded to the :class:`MiniAgent` constructor (e.g.
    ``approval_policy``, ``max_steps``, ``read_only``).
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    ws = WorkspaceContext.build(str(workdir))
    store = SessionStore(workdir / ".mini-coding-agent" / "sessions")
    client = build_model_client_from_env()

    params = {"approval_policy": "auto", "max_steps": 12}
    params.update(kwargs)
    return MiniAgent(
        model_client=client,
        workspace=ws,
        session_store=store,
        **params,
    )
