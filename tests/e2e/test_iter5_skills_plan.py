"""Iteration 5 e2e: skill metadata enrichment + plan re-consultation."""

from codelet import (
    FakeModelClient,
    MiniAgent,
    SessionStore,
    WorkspaceContext,
)
from codelet.skills import discover_skills, load_skill_body, render_skill_manifest


def build_agent(tmp_path, outputs, **kwargs):
    ws = WorkspaceContext.build(str(tmp_path))
    store = SessionStore(tmp_path / ".mini-coding-agent" / "sessions")
    client = FakeModelClient(outputs)
    agent = MiniAgent(
        model_client=client,
        workspace=ws,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )
    return agent, client


def _make_skill(tmp_path, front_matter, body="Do the thing.\n"):
    skill_dir = tmp_path / ".mini-coding-agent" / "skills" / "demo"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{front_matter}\n---\n{body}", encoding="utf-8"
    )
    return skill_dir


def test_skill_metadata_parsed_and_surfaced(tmp_path):
    _make_skill(
        tmp_path,
        front_matter=(
            "name: demo\n"
            "description: A demo skill.\n"
            "when_to_use: the user asks for a demo\n"
            "argument_hint: <topic>\n"
            "allowed_tools: read_file, write_file\n"
        ),
    )
    skills = discover_skills(tmp_path)
    assert len(skills) == 1
    skill = skills[0]

    assert skill.when_to_use == "the user asks for a demo"
    assert skill.argument_hint == "<topic>"
    assert skill.allowed_tools == ["read_file", "write_file"]

    # whenToUse hint appears in the short manifest.
    manifest = render_skill_manifest(skills)
    assert "use when: the user asks for a demo" in manifest

    # argument hint + allowed tools appear in the loaded body.
    body = load_skill_body(skills, "demo")
    assert "Argument hint: <topic>" in body
    assert "Allowed tools for this skill: read_file, write_file" in body


def test_skill_metadata_camelcase_aliases(tmp_path):
    _make_skill(
        tmp_path,
        front_matter=(
            "name: demo\n"
            "description: A demo skill.\n"
            "whenToUse: editing docs\n"
            "argumentHint: <file>\n"
            "allowedTools: read_file\n"
        ),
    )
    skills = discover_skills(tmp_path)
    skill = skills[0]
    assert skill.when_to_use == "editing docs"
    assert skill.argument_hint == "<file>"
    assert skill.allowed_tools == ["read_file"]


def test_skill_without_metadata_is_backward_compatible(tmp_path):
    _make_skill(
        tmp_path,
        front_matter="name: demo\ndescription: A demo skill.",
    )
    skills = discover_skills(tmp_path)
    skill = skills[0]
    assert skill.when_to_use == ""
    assert skill.argument_hint == ""
    assert skill.allowed_tools == []
    # Manifest is the plain one-liner with no trailing hint.
    assert skill.manifest() == "- demo: A demo skill."


def test_plan_is_injected_into_subsequent_prompts(tmp_path):
    outputs = [
        '<tool>{"name":"decompose","args":{"goal":"Build it",'
        '"steps":["First step","Second step"]}}</tool>',
        "<final>done</final>",
    ]
    agent, client = build_agent(tmp_path, outputs, max_steps=20)

    answer = agent.ask("please plan and build")

    assert answer == "done"
    # The plan was recorded by decompose.
    assert agent.session["plan"]["goal"] == "Build it"
    # The second prompt (after decompose) must re-surface the plan.
    second_prompt = client.prompts[-1]
    assert "<plan>" in second_prompt
    assert "Active plan for: Build it" in second_prompt
    assert "1. First step" in second_prompt
    assert "2. Second step" in second_prompt


def test_no_plan_means_no_plan_block(tmp_path):
    outputs = ["<final>done</final>"]
    agent, client = build_agent(tmp_path, outputs)

    agent.ask("just answer")

    assert "<plan>" not in client.prompts[-1]
