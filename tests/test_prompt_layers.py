"""Tests for the six-layer XML-tagged prompt architecture."""

import re

import pytest

from mini_coding_agent import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
    build_prefix,
)


def make_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return WorkspaceContext.build(tmp_path)


def make_agent(tmp_path, **kwargs):
    workspace = make_workspace(tmp_path)
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    return MiniAgent(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def test_prefix_contains_six_named_xml_layers(tmp_path):
    agent = make_agent(tmp_path)
    prefix = agent.prefix
    # Always-present stable layers.
    assert "<agent-identity>" in prefix and "</agent-identity>" in prefix
    assert "<system-defaults>" in prefix and "</system-defaults>" in prefix
    assert "<workspace>" in prefix and "</workspace>" in prefix
    # Layers whose content depends on context.
    # In a default agent, delegation is available (depth=0 < max_depth=1) and
    # there is no AGENTS.md or override text, so we expect:
    assert "<coordinator>" in prefix
    assert "<project-rules>" not in prefix
    assert "<override>" not in prefix


def test_layer_order_is_immutable_first_volatile_last(tmp_path):
    agent = make_agent(tmp_path)
    prefix = agent.prefix
    # All present layers must appear in this exact order:
    expected_order = [
        "<agent-identity>",
        "<system-defaults>",
        "<coordinator>",
        "<workspace>",
    ]
    positions = [prefix.find(tag) for tag in expected_order]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), "stable layers must appear before volatile ones"


def test_full_prompt_volatile_layers_come_last(tmp_path):
    agent = make_agent(tmp_path)
    prompt = agent.prompt("inspect README")
    # Volatile layers exist and are wrapped in XML.
    assert "<memory>" in prompt and "</memory>" in prompt
    assert "<transcript>" in prompt and "</transcript>" in prompt
    assert "<request>" in prompt and "</request>" in prompt
    # Request must be the very last tag - the most volatile content.
    assert prompt.rfind("<request>") > prompt.rfind("<memory>")
    assert prompt.rfind("<request>") > prompt.rfind("<transcript>")
    assert prompt.rfind("<request>") > prompt.rfind("<workspace>")
    # User message body is inside <request>.
    assert "inspect README" in prompt


def test_prefix_is_cache_friendly_across_turns(tmp_path):
    """Stable prefix bytes must not change when only the user message changes."""
    agent = make_agent(tmp_path)
    p1 = agent.prompt("first request")
    p2 = agent.prompt("second request")
    # The prefix portion (everything before <memory>) must be byte-identical.
    cut1 = p1.find("<memory>") if "<memory>" in p1 else p1.find("<transcript>")
    cut2 = p2.find("<memory>") if "<memory>" in p2 else p2.find("<transcript>")
    assert cut1 > 0 and p1[:cut1] == p2[:cut2]


def test_project_rules_layer_loads_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# Project rules\n- Always include type hints.\n",
        encoding="utf-8",
    )
    agent = make_agent(tmp_path)
    assert "<project-rules>" in agent.prefix
    assert "Always include type hints" in agent.prefix
    # Identity should appear before project rules (stability ordering).
    assert agent.prefix.find("<agent-identity>") < agent.prefix.find("<project-rules>")


def test_override_layer_emitted_when_config_provides_text(tmp_path):
    agent = make_agent(
        tmp_path,
        config={"prompts": {"override": "Always run the tests after writing code."}},
    )
    assert "<override>" in agent.prefix
    assert "Always run the tests after writing code." in agent.prefix


def test_coordinator_layer_omitted_when_delegation_disabled(tmp_path):
    # max_depth=0 disables the delegate tool, so coordinator should disappear.
    agent = make_agent(tmp_path, max_depth=0)
    assert "<coordinator>" not in agent.prefix
    assert "delegate" not in agent.tools


def test_existing_labels_remain_flush_left_inside_their_tags(tmp_path):
    """The historical flush-left labels must still appear for log scrapers."""
    agent = make_agent(tmp_path)
    prompt = agent.prompt("hi")
    for label in ("Rules:", "Tools:", "Valid response examples:", "Workspace:",
                  "Memory:", "Transcript:", "Current user request:"):
        assert re.search(rf"^{re.escape(label)}$", prompt, re.MULTILINE), label


def test_build_prefix_uses_configured_rules_and_examples(tmp_path):
    workspace = make_workspace(tmp_path)
    custom_cfg = {
        "agent_identity": "I am Custom Agent.",
        "rules": ["Custom rule one.", "Custom rule two."],
        "examples": {"list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>'},
        "project_rules": "",
        "coordinator": "",
        "override": "",
    }
    tools = {
        "list_files": {
            "schema": {"path": "str='.'"},
            "risky": False,
            "description": "List files.",
            "run": lambda args: "",
        }
    }
    prefix = build_prefix(custom_cfg, tools, workspace.text())
    assert "I am Custom Agent." in prefix
    assert "Custom rule one." in prefix
    assert "Custom rule two." in prefix
    # Only the tools present in the registry get listed.
    assert "list_files" in prefix
    assert "run_shell" not in prefix


def test_examples_block_only_lists_active_tools(tmp_path):
    agent = make_agent(tmp_path, allowed_ops={"read"})
    # Examples block lives inside <system-defaults>. Extract just the
    # "Valid response examples:" section and verify it.
    sd_start = agent.prefix.index("<system-defaults>")
    sd_end = agent.prefix.index("</system-defaults>")
    defaults = agent.prefix[sd_start:sd_end]
    examples_section = defaults.split("Valid response examples:", 1)[1]
    # write/patch/shell/python example tags must be suppressed when the tools
    # are absent.
    assert 'name":"write_file"' not in examples_section
    assert 'name":"patch_file"' not in examples_section
    assert 'name":"run_shell"' not in examples_section
    assert 'name":"run_python"' not in examples_section
    # Read-tool examples are present.
    assert 'name":"list_files"' in examples_section
    assert 'name":"read_file"' in examples_section


def test_custom_retry_notice_template_used(tmp_path):
    agent = make_agent(
        tmp_path,
        config={
            "prompts": {
                "retry_notice": "CUSTOM-NOTICE{problem_suffix}. Try again."
            }
        },
    )

    # Feed an empty model output; the agent should record the custom notice.
    agent.model_client.outputs = ["", "<final>ok</final>"]
    answer = agent.ask("do something")
    assert answer == "ok"
    notices = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert any("CUSTOM-NOTICE" in note for note in notices)


def test_configurable_max_steps_from_config_used(tmp_path):
    """A config-supplied max_steps applies when the caller doesn't override."""
    agent = make_agent(tmp_path, config={"harness": {"max_steps": 3}})
    assert agent.max_steps == 3


def test_explicit_kwarg_beats_config(tmp_path):
    """Explicit kwargs to MiniAgent must override config values."""
    agent = make_agent(tmp_path, config={"harness": {"max_steps": 3}}, max_steps=7)
    assert agent.max_steps == 7
