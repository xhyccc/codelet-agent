"""Shared fixtures for integration tests.

Integration tests require environment variables (API key, model) set in a .env
file or exported directly. They are skipped automatically when credentials are
not available.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codelet import MiniAgent, OpenAIModelClient, SessionStore, WorkspaceContext
from codelet.env_config import discover_env_file, load_env_into_environ


# ---------------------------------------------------------------------------
# Automatically load .env from the repo root (if present) before tests run.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_env_loaded():
    """Load .env from the repository root into os.environ (idempotent)."""
    env_path = discover_env_file(str(_REPO_ROOT))
    if env_path:
        load_env_into_environ(env_path)


_ensure_env_loaded()


# ---------------------------------------------------------------------------
# Markers & skip conditions
# ---------------------------------------------------------------------------

def _has_soffice() -> bool:
    import shutil
    return shutil.which("soffice") is not None


requires_api_key = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY") and not os.environ.get("LLM_API_KEY"),
    reason="No API key available (set OPENAI_API_KEY or LLM_API_KEY in .env)",
)

requires_libreoffice = pytest.mark.skipif(
    not _has_soffice(),
    reason="LibreOffice (soffice) not installed on PATH",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_key() -> str:
    """Return the API key from environment."""
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    if not key:
        pytest.skip("No API key available")
    return key


@pytest.fixture()
def model_name() -> str:
    """Return the model name from environment, defaulting to gpt-4o-mini."""
    return os.environ.get("LLM_MODEL") or "gpt-4o-mini"


@pytest.fixture()
def base_url() -> str | None:
    """Return the base URL from environment (if any)."""
    return os.environ.get("LLM_BASE_URL") or None


@pytest.fixture()
def real_model_client(api_key, model_name, base_url):
    """Build an OpenAIModelClient from .env configuration."""
    return OpenAIModelClient(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=0.0,
        top_p=1.0,
        timeout=60,
    )


@pytest.fixture()
def workspace(tmp_path):
    """Create a minimal workspace in a temp directory."""
    (tmp_path / "README.md").write_text("# Test Project\n", encoding="utf-8")
    return WorkspaceContext.build(str(tmp_path))


@pytest.fixture()
def integration_agent(real_model_client, workspace, tmp_path):
    """Build a MiniAgent backed by a real LLM for integration testing."""
    store = SessionStore(tmp_path / ".codelet" / "sessions")
    return MiniAgent(
        model_client=real_model_client,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=5,
    )
