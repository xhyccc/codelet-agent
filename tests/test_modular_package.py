"""Tests for the new ``glob`` tool and the modular package layout."""

import pytest

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
)


def build_agent(tmp_path, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    kwargs.setdefault("approval_policy", "auto")
    return MiniAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        **kwargs,
    )


def test_glob_tool_present_by_default(tmp_path):
    agent = build_agent(tmp_path)
    assert "glob" in agent.tools


def test_glob_tool_omitted_when_read_disabled(tmp_path):
    agent = build_agent(tmp_path, allowed_ops={"write"})
    assert "glob" not in agent.tools


def test_glob_matches_python_files(tmp_path):
    (tmp_path / "a.py").write_text("# a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# b\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("# c\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("nope\n", encoding="utf-8")

    agent = build_agent(tmp_path)
    result = agent.run_tool("glob", {"pattern": "**/*.py"})

    assert "[F] a.py" in result
    assert "[F] b.py" in result
    assert "sub/c.py" in result
    assert "ignore.txt" not in result


def test_glob_returns_no_matches_message(tmp_path):
    agent = build_agent(tmp_path)
    result = agent.run_tool("glob", {"pattern": "**/*.does-not-exist"})
    assert result == "(no matches)"


def test_glob_skips_ignored_directories(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("# secret\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("# src\n", encoding="utf-8")

    agent = build_agent(tmp_path)
    result = agent.run_tool("glob", {"pattern": "**/*.py"})

    assert "src.py" in result
    assert ".git" not in result


def test_glob_rejects_empty_pattern(tmp_path):
    agent = build_agent(tmp_path)
    result = agent.run_tool("glob", {"pattern": ""})
    assert result.startswith("error: invalid arguments for glob")


def test_glob_rejects_path_escape(tmp_path):
    agent = build_agent(tmp_path)
    result = agent.run_tool("glob", {"pattern": "*.py", "path": "../"})
    assert result.startswith("error: invalid arguments for glob")
    assert "path escapes workspace" in result


def test_module_layout_exposes_submodules():
    """Smoke-test the public submodule layout."""
    from codelet import (
        agent as agent_mod,
        cli as cli_mod,
        clients,
        config as config_mod,
        parsing,
        prompt,
        providers,
        sandbox,
        sessions,
        tools,
        utils,
        welcome,
        workspace as workspace_mod,
    )

    # Sanity: every module must define at least one of the symbols re-exported
    # by codelet.__init__.
    assert hasattr(agent_mod, "MiniAgent")
    assert hasattr(cli_mod, "main")
    assert hasattr(clients, "OllamaModelClient")
    assert hasattr(config_mod, "load_config")
    assert hasattr(parsing, "parse_model_output")
    assert hasattr(prompt, "build_prefix")
    assert hasattr(providers, "LLM_PROVIDER_PRESETS")
    assert hasattr(sandbox, "sandbox_check_shell")
    assert hasattr(sessions, "SessionStore")
    assert hasattr(tools, "ToolRegistry")
    assert hasattr(utils, "clip")
    assert hasattr(welcome, "build_welcome")
    assert hasattr(workspace_mod, "WorkspaceContext")


def test_public_api_remains_importable_from_package_root():
    """Pre-refactor imports must still work for backward compatibility."""
    from codelet import (  # noqa: F401
        ALL_TOOL_OPS,
        BUILTIN_DEFAULTS,
        FakeModelClient,
        LLM_PROVIDER_PRESETS,
        MiniAgent,
        OllamaModelClient,
        OpenAIModelClient,
        SessionStore,
        WorkspaceContext,
        _post_process_args,
        build_agent,
        build_arg_parser,
        build_welcome,
        clip,
        load_config,
        main,
        now,
        resolve_provider_preset,
        sandbox_check_python,
        sandbox_check_shell,
        sandbox_filter_env,
    )


def test_config_flag_in_cli_parser(tmp_path):
    from codelet import _post_process_args, build_arg_parser

    parser = build_arg_parser()
    args = parser.parse_args(["--config", str(tmp_path / "custom.yaml"), "--cwd", str(tmp_path)])
    args = _post_process_args(args)
    assert args.config == str(tmp_path / "custom.yaml")


def test_workspace_yaml_config_picked_up_by_cli(tmp_path, monkeypatch):
    """A .mini-coding-agent/config.yaml in the workspace is auto-discovered."""
    pytest.importorskip("yaml")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    nested = tmp_path / ".mini-coding-agent"
    nested.mkdir()
    (nested / "config.yaml").write_text(
        "harness:\n  max_steps: 13\n  max_new_tokens: 222\n",
        encoding="utf-8",
    )

    from codelet import _post_process_args, build_arg_parser
    from codelet import build_agent as cli_build_agent

    parser = build_arg_parser()
    args = parser.parse_args(["--cwd", str(tmp_path)])
    args = _post_process_args(args)
    args.approval = "auto"

    # Use Ollama backend (no API key required); model_client is built but
    # won't actually run.
    agent = cli_build_agent(args)
    assert agent.max_steps == 13
    assert agent.max_new_tokens == 222


def test_user_config_flag_overrides_workspace_yaml(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    nested = tmp_path / ".mini-coding-agent"
    nested.mkdir()
    (nested / "config.yaml").write_text(
        "harness:\n  max_steps: 13\n", encoding="utf-8"
    )
    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text("harness:\n  max_steps: 88\n", encoding="utf-8")

    from codelet import _post_process_args, build_arg_parser
    from codelet import build_agent as cli_build_agent

    parser = build_arg_parser()
    args = parser.parse_args(["--cwd", str(tmp_path), "--config", str(user_yaml)])
    args = _post_process_args(args)
    args.approval = "auto"

    agent = cli_build_agent(args)
    assert agent.max_steps == 88
