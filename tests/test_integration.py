"""Integration tests: compaction, memory files, and .env wired through MiniAgent / CLI."""

from unittest.mock import patch

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
    build_arg_parser,
    _post_process_args,
)
from codelet.cli import build_agent as cli_build_agent


def _workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def _agent(tmp_path, outputs, **kwargs):
    ws = _workspace(tmp_path)
    store = SessionStore(tmp_path / ".codelet" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient(outputs),
        workspace=ws,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


# ----- Memory files included in prefix -------------------------------------


def test_memory_files_appear_in_project_rules_layer(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Project memory\nuse pytest\n", encoding="utf-8")
    (tmp_path / "CLAUDE.local.md").write_text("# Workspace notes\nlocal-only fact\n", encoding="utf-8")
    config = {
        "memory_files": {
            "enabled": True,
            "max_files": 5,
            "global_roots": [],
            "user_roots": [],
        }
    }
    agent = _agent(tmp_path, ["<final>ok</final>"], config=config)
    assert "Memory files:" in agent.prefix
    assert "CLAUDE.md" in agent.prefix
    assert "use pytest" in agent.prefix
    assert "local-only fact" in agent.prefix


def test_memory_files_can_be_disabled(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Project memory\nfact\n", encoding="utf-8")
    config = {"memory_files": {"enabled": False}}
    agent = _agent(tmp_path, ["<final>ok</final>"], config=config)
    assert "Memory files:" not in agent.prefix
    assert agent.memory_files == []


# ----- Baseline verification -----------------------------------------------


def test_agent_seeds_baseline_on_first_run(tmp_path):
    agent = _agent(tmp_path, ["<final>ok</final>"])
    assert agent.session["baseline"] is not None
    assert agent.baseline_drift == []


def test_agent_resume_detects_external_file_change(tmp_path):
    (tmp_path / "AGENTS.md").write_text("v1", encoding="utf-8")
    agent = _agent(tmp_path, ["<final>first</final>"])
    agent.ask("hello")
    session_id = agent.session["id"]

    # Simulate external drift.
    (tmp_path / "AGENTS.md").write_text("v2 different", encoding="utf-8")

    resumed = MiniAgent.from_session(
        model_client=FakeModelClient(["<final>second</final>"]),
        workspace=agent.workspace,
        session_store=agent.session_store,
        session_id=session_id,
        approval_policy="auto",
    )
    assert any("AGENTS.md" in line for line in resumed.baseline_drift)


# ----- Compaction wiring ---------------------------------------------------


def test_history_text_triggers_compaction_when_oversized(tmp_path):
    config = {
        "harness": {
            "max_history": 100000,
            "compaction": {
                "target_chars": 500,
                "preserve_recent": 2,
                "microcompact_clip": 50,
                "auto_compaction": False,
            },
        }
    }
    agent = _agent(tmp_path, [], config=config)
    # Inject a synthetic, very large history bypassing the real loop.
    big = "x" * 5000
    for i in range(8):
        agent.session["history"].append(
            {"role": "tool", "name": "run_shell", "args": {}, "content": f"out{i}\n" + big}
        )
    text = agent.history_text()
    # Some stage(s) must have been applied.
    assert agent.last_compaction_stages
    # The rendered transcript must be smaller than the raw concatenation.
    assert len(text) < 8 * 5000


def test_history_text_no_compaction_when_small(tmp_path):
    agent = _agent(tmp_path, [])
    agent.session["history"].append(
        {"role": "user", "content": "tiny", "created_at": "0"}
    )
    agent.history_text()
    assert agent.last_compaction_stages == []


# ----- .env wiring through the CLI -----------------------------------------


def test_cli_build_agent_reads_env_file(tmp_path):
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=kimi\n"
        "KIMI_API_KEY=sk-from-env\n"
        "LLM_MODEL=moonshot-v1-32k\n"
        "MINI_AGENT_MAX_STEPS=4\n",
        encoding="utf-8",
    )

    parser = build_arg_parser()
    args = parser.parse_args(["--cwd", str(tmp_path)])
    args = _post_process_args(args)
    # Don't carry the real OPENAI_API_KEY across into this test.
    with patch.dict("os.environ", {"OPENAI_API_KEY": "", "MOONSHOT_API_KEY": ""}, clear=False):
        # ``openai`` package may not be installed for OpenAI backend; we only
        # care that build_agent set up the right attributes before instantiating
        # the client.
        agent = cli_build_agent(args)

    assert args.provider == "kimi"
    assert args.model == "moonshot-v1-32k"
    assert args.openai_api_key == "sk-from-env"
    assert args.max_steps == 4
    # YAML harness slice merged.
    assert agent.config["harness"]["max_steps"] == 4


def test_cli_explicit_flag_beats_env_file(tmp_path):
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")
    (tmp_path / ".env").write_text("MINI_AGENT_MAX_STEPS=99\n", encoding="utf-8")
    parser = build_arg_parser()
    args = parser.parse_args(["--cwd", str(tmp_path), "--max-steps", "3"])
    args = _post_process_args(args)
    agent = cli_build_agent(args)
    assert args.max_steps == 3
    assert agent.max_steps == 3


def test_cli_works_without_env_file(tmp_path):
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")
    parser = build_arg_parser()
    args = parser.parse_args(["--cwd", str(tmp_path)])
    args = _post_process_args(args)
    # Should not raise; .env auto-discovery returns None.
    agent = cli_build_agent(args)
    assert agent.max_steps == 6
