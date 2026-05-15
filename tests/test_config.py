"""Tests for the YAML-backed configuration loader."""

from pathlib import Path

import pytest

from mini_coding_agent.config import (
    BUILTIN_DEFAULTS,
    deep_merge,
    discover_workspace_config,
    load_config,
    load_packaged_defaults,
    load_project_rules,
)


def test_builtin_defaults_match_packaged_yaml():
    """The BUILTIN_DEFAULTS fallback must mirror config/default.yaml.

    PyYAML is a dev dependency; if it isn't installed we just rely on the
    Python fallback and there is nothing to compare against.
    """
    pytest.importorskip("yaml")
    yaml_defaults = load_packaged_defaults()
    # Both must contain the same top-level keys.
    assert set(yaml_defaults.keys()) == set(BUILTIN_DEFAULTS.keys())
    # Critical harness fields must agree.
    for key in ("max_steps", "max_new_tokens", "max_depth", "sandbox", "approval"):
        assert yaml_defaults["harness"][key] == BUILTIN_DEFAULTS["harness"][key]
    # All rules must match.
    assert yaml_defaults["prompts"]["rules"] == BUILTIN_DEFAULTS["prompts"]["rules"]
    # Examples must cover the same tool set.
    assert set(yaml_defaults["prompts"]["examples"]) == set(BUILTIN_DEFAULTS["prompts"]["examples"])


def test_deep_merge_preserves_nested_keys():
    base = {"a": {"x": 1, "y": 2}, "b": [1, 2]}
    override = {"a": {"y": 99, "z": 3}, "c": "new"}
    merged = deep_merge(base, override)
    assert merged == {"a": {"x": 1, "y": 99, "z": 3}, "b": [1, 2], "c": "new"}
    # Base is left untouched.
    assert base == {"a": {"x": 1, "y": 2}, "b": [1, 2]}


def test_deep_merge_replaces_lists_wholesale():
    base = {"items": [1, 2, 3]}
    override = {"items": [9]}
    assert deep_merge(base, override) == {"items": [9]}


def test_load_config_applies_user_yaml(tmp_path):
    pytest.importorskip("yaml")
    user_path = tmp_path / "custom.yaml"
    user_path.write_text(
        "harness:\n"
        "  max_steps: 99\n"
        "  approval: never\n"
        "prompts:\n"
        "  override: |\n"
        "    Be especially terse.\n",
        encoding="utf-8",
    )

    config = load_config(user_config_path=user_path)

    assert config["harness"]["max_steps"] == 99
    assert config["harness"]["approval"] == "never"
    assert "Be especially terse." in config["prompts"]["override"]
    # Untouched defaults survive.
    assert config["harness"]["max_new_tokens"] == BUILTIN_DEFAULTS["harness"]["max_new_tokens"]
    assert config["prompts"]["rules"] == BUILTIN_DEFAULTS["prompts"]["rules"]


def test_load_config_workspace_then_user(tmp_path):
    pytest.importorskip("yaml")
    workspace_yaml = tmp_path / "ws.yaml"
    workspace_yaml.write_text(
        "harness:\n  max_steps: 11\n  max_new_tokens: 111\n",
        encoding="utf-8",
    )
    user_yaml = tmp_path / "user.yaml"
    user_yaml.write_text("harness:\n  max_steps: 22\n", encoding="utf-8")

    config = load_config(user_config_path=user_yaml, workspace_config_path=workspace_yaml)

    # User config wins over workspace.
    assert config["harness"]["max_steps"] == 22
    # Workspace config still wins over packaged default for unmerged keys.
    assert config["harness"]["max_new_tokens"] == 111


def test_load_config_no_args_returns_defaults():
    config = load_config()
    assert config["harness"]["max_steps"] == BUILTIN_DEFAULTS["harness"]["max_steps"]
    assert config["prompts"]["agent_identity"] == BUILTIN_DEFAULTS["prompts"]["agent_identity"]


def test_load_config_rejects_non_mapping_file(tmp_path):
    pytest.importorskip("yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="YAML mapping"):
        load_config(user_config_path=bad)


def test_discover_workspace_config_finds_dotfile(tmp_path):
    nested = tmp_path / ".mini-coding-agent"
    nested.mkdir()
    cfg = nested / "config.yaml"
    cfg.write_text("harness:\n  max_steps: 7\n", encoding="utf-8")
    assert discover_workspace_config(tmp_path) == cfg


def test_discover_workspace_config_returns_none_when_missing(tmp_path):
    assert discover_workspace_config(tmp_path) is None


def test_load_project_rules_concatenates_files(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agent rules\nBe terse.\n", encoding="utf-8")
    nested = tmp_path / ".mini-coding-agent"
    nested.mkdir()
    (nested / "rules.md").write_text("Always write tests.\n", encoding="utf-8")

    text = load_project_rules(tmp_path, ["AGENTS.md", ".mini-coding-agent/rules.md"])
    assert "# AGENTS.md" in text
    assert "Be terse." in text
    assert "# .mini-coding-agent/rules.md" in text
    assert "Always write tests." in text


def test_load_project_rules_skips_missing_files(tmp_path):
    assert load_project_rules(tmp_path, ["does-not-exist.md"]) == ""


def test_load_project_rules_handles_empty_inputs(tmp_path):
    assert load_project_rules(tmp_path, []) == ""
    assert load_project_rules(None, ["AGENTS.md"]) == ""
