"""Tests for .env config loading."""

import os
from unittest.mock import patch

from codelet.env_config import (
    discover_env_file,
    env_to_overrides,
    load_env_config,
    load_env_into_environ,
    parse_env_file,
    resolve_api_key,
)


# ----- parse_env_file ------------------------------------------------------


def test_parse_env_file_basic(tmp_path):
    p = tmp_path / ".env"
    p.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
    assert parse_env_file(p) == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_handles_comments_and_blank_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text(
        "# leading comment\n\n"
        "FOO=bar    # inline comment\n"
        "QUOTED=\"v a l\"\n"
        "SQUOTED='v2'\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    out = parse_env_file(p)
    assert out["FOO"] == "bar"
    assert out["QUOTED"] == "v a l"
    assert out["SQUOTED"] == "v2"
    assert out["EMPTY"] == ""


def test_parse_env_file_strips_export_prefix(tmp_path):
    p = tmp_path / ".env"
    p.write_text("export A=1\nexport  B=2\n", encoding="utf-8")
    out = parse_env_file(p)
    assert out == {"A": "1", "B": "2"}


def test_parse_env_file_preserves_hash_inside_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('KEY="value#with#hash"\n', encoding="utf-8")
    assert parse_env_file(p)["KEY"] == "value#with#hash"


def test_parse_env_file_missing_returns_empty(tmp_path):
    assert parse_env_file(tmp_path / "missing.env") == {}


def test_parse_env_file_skips_malformed_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("OK=1\nbroken line\nALSO=2\n=novalue\n", encoding="utf-8")
    out = parse_env_file(p)
    assert out == {"OK": "1", "ALSO": "2"}


# ----- discover_env_file ---------------------------------------------------


def test_discover_env_file_finds_dotfile(tmp_path):
    (tmp_path / ".env").write_text("X=1", encoding="utf-8")
    assert discover_env_file(tmp_path) == (tmp_path / ".env").resolve()


def test_discover_env_file_missing(tmp_path):
    assert discover_env_file(tmp_path) is None


# ----- load_env_into_environ ----------------------------------------------


def test_load_env_into_environ_does_not_overwrite_by_default():
    with patch.dict(os.environ, {"X_TEST_KEY": "real"}, clear=False):
        load_env_into_environ({"X_TEST_KEY": "fromfile"})
        assert os.environ["X_TEST_KEY"] == "real"


def test_load_env_into_environ_overrides_when_requested():
    with patch.dict(os.environ, {"X_TEST_KEY": "real"}, clear=False):
        load_env_into_environ({"X_TEST_KEY": "fromfile"}, override=True)
        assert os.environ["X_TEST_KEY"] == "fromfile"


def test_load_env_into_environ_sets_missing_keys():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("X_NEW_KEY", None)
        load_env_into_environ({"X_NEW_KEY": "v"})
        assert os.environ["X_NEW_KEY"] == "v"


# ----- resolve_api_key -----------------------------------------------------


def test_resolve_api_key_prefers_llm_api_key():
    env = {"LLM_API_KEY": "winner", "KIMI_API_KEY": "loser"}
    assert resolve_api_key("kimi", env) == "winner"


def test_resolve_api_key_uses_kimi_then_moonshot():
    assert resolve_api_key("kimi", {"KIMI_API_KEY": "k"}) == "k"
    assert resolve_api_key("kimi", {"MOONSHOT_API_KEY": "m"}) == "m"
    assert resolve_api_key("moonshot", {"KIMI_API_KEY": "k"}) == "k"


def test_resolve_api_key_zhipu_uses_zhipu():
    assert resolve_api_key("zhipu", {"ZHIPU_API_KEY": "z"}) == "z"
    assert resolve_api_key("glm", {"ZHIPU_API_KEY": "z"}) == "z"


def test_resolve_api_key_falls_back_to_openai():
    assert resolve_api_key("kimi", {"OPENAI_API_KEY": "o"}) == "o"


def test_resolve_api_key_returns_none_when_nothing_set():
    assert resolve_api_key("custom", {}) is None


# ----- env_to_overrides ----------------------------------------------------


def test_env_to_overrides_extracts_provider_and_keys():
    env = {
        "LLM_PROVIDER": "Kimi",
        "KIMI_API_KEY": "sk-x",
        "LLM_MODEL": "moonshot-v1-32k",
    }
    out = env_to_overrides(env)
    assert out["cli"]["provider"] == "kimi"
    assert out["cli"]["openai_api_key"] == "sk-x"
    assert out["cli"]["model"] == "moonshot-v1-32k"


def test_env_to_overrides_custom_provider_uses_base_url():
    env = {
        "LLM_PROVIDER": "custom",
        "LLM_API_KEY": "sk-z",
        "LLM_BASE_URL": "https://x/v1",
        "LLM_MODEL": "m",
    }
    out = env_to_overrides(env)
    assert out["cli"]["provider"] == "custom"
    assert out["cli"]["openai_base_url"] == "https://x/v1"
    assert out["cli"]["openai_api_key"] == "sk-z"
    assert out["cli"]["model"] == "m"


def test_env_to_overrides_harness_fields():
    env = {
        "MINI_AGENT_MAX_STEPS": "25",
        "MINI_AGENT_OPENAI_TIMEOUT": "300",
        "MINI_AGENT_MAX_NEW_TOKENS": "8192",
    }
    out = env_to_overrides(env)
    assert out["cli"]["max_steps"] == 25
    assert out["cli"]["openai_timeout"] == 300
    assert out["cli"]["max_new_tokens"] == 8192
    assert out["harness"] == {
        "max_steps": 25,
        "openai_timeout": 300,
        "max_new_tokens": 8192,
    }


def test_env_to_overrides_ignores_non_numeric_harness_values():
    env = {"MINI_AGENT_MAX_STEPS": "lots"}
    out = env_to_overrides(env)
    assert "max_steps" not in out["cli"]
    assert out["harness"] == {}


def test_env_to_overrides_empty_env():
    assert env_to_overrides({}) == {"cli": {}, "harness": {}}


# ----- load_env_config -----------------------------------------------------


def test_load_env_config_returns_overrides(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_PROVIDER=kimi\n"
        "KIMI_API_KEY=sk-x\n"
        "MINI_AGENT_MAX_STEPS=12\n",
        encoding="utf-8",
    )
    env, overrides = load_env_config(path=env_file)
    assert env["LLM_PROVIDER"] == "kimi"
    assert overrides["cli"]["provider"] == "kimi"
    assert overrides["cli"]["openai_api_key"] == "sk-x"
    assert overrides["harness"]["max_steps"] == 12


def test_load_env_config_no_file(tmp_path):
    env, overrides = load_env_config(cwd=tmp_path)
    assert env == {}
    assert overrides == {"cli": {}, "harness": {}}


def test_load_env_config_autodiscovers_dotfile_at_cwd(tmp_path):
    (tmp_path / ".env").write_text("LLM_PROVIDER=zhipu\nZHIPU_API_KEY=z\n", encoding="utf-8")
    env, overrides = load_env_config(cwd=tmp_path)
    assert env["LLM_PROVIDER"] == "zhipu"
    assert overrides["cli"]["provider"] == "zhipu"
    assert overrides["cli"]["openai_api_key"] == "z"
