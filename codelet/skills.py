"""Progressive-disclosure skills for codelet.

A *skill* is just a directory under ``.codelet/skills/`` with
a ``SKILL.md`` file whose front-matter (or first commented block)
declares a ``name`` and ``description``::

    ---
    name: changelog-writer
    description: Generate a CHANGELOG.md entry from recent git history.
    ---
    # When called via load_skill("changelog-writer"), the body below
    # plus any sibling files becomes available to the agent.
    ...

At startup we only inject **name + description** for each discovered
skill into the system prompt -- this is *progressive disclosure*: the
agent learns that skills exist, but their bodies cost nothing until it
calls the :func:`load_skill` tool.

There is no YAML library dependency: we parse the front-matter manually.
If front-matter is missing we fall back to the first line of the file
for the description and the directory name for the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


SKILL_DIR_NAME = ".codelet/skills"
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    path: Path  # directory containing SKILL.md
    body: str = ""
    assets: List[str] = field(default_factory=list)
    when_to_use: str = ""
    argument_hint: str = ""
    allowed_tools: List[str] = field(default_factory=list)
    effort: str = "medium"  # low | medium | high

    def manifest(self) -> str:
        """One-line ``- name: description`` for the prompt prefix.

        When the skill declares ``when_to_use`` metadata it is appended as a
        short ``(use when: ...)`` hint so the model can decide *whether* to
        load the skill without paying for its full body.
        """
        line = f"- {self.name}: {self.description}"
        if self.when_to_use:
            line += f" (use when: {self.when_to_use})"
        return line


def _split_list(value) -> List[str]:
    """Parse a metadata value into a list of strings.

    Accepts a JSON-ish ``[a, b]`` form or a comma/space separated string.
    """
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value or "").strip().strip("[]")
    if not text:
        return []
    sep = "," if "," in text else None
    parts = text.split(sep)
    return [p.strip().strip("\"'") for p in parts if p.strip().strip("\"'")]



def _parse_front_matter(text: str) -> Dict[str, str]:
    m = _FRONT_MATTER_RE.match(text)
    if not m:
        return {}
    out: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("\"'")
    return out


def _parse_skill_file(skill_dir: Path) -> Optional[Skill]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    fm = _parse_front_matter(text)
    body = _FRONT_MATTER_RE.sub("", text, count=1).strip()
    name = fm.get("name") or skill_dir.name
    description = fm.get("description") or (body.splitlines()[0] if body else "")
    # Optional enrichment metadata (snake_case or camelCase accepted).
    when_to_use = fm.get("when_to_use") or fm.get("whenToUse") or ""
    argument_hint = fm.get("argument_hint") or fm.get("argumentHint") or ""
    allowed_tools = _split_list(fm.get("allowed_tools") or fm.get("allowedTools"))
    effort = fm.get("effort") or "medium"
    # Asset list: every sibling file under the skill dir except SKILL.md.
    assets = sorted(
        str(p.relative_to(skill_dir))
        for p in skill_dir.rglob("*")
        if p.is_file() and p.name != "SKILL.md"
    )
    return Skill(
        name=name,
        description=description,
        path=skill_dir,
        body=body,
        assets=assets,
        when_to_use=when_to_use,
        argument_hint=argument_hint,
        allowed_tools=allowed_tools,
        effort=effort,
    )


def discover_skills(repo_root) -> List[Skill]:
    """Scan ``<repo>/.codelet/skills/*/SKILL.md`` and ``~/.claude/skills/*/SKILL.md``."""
    skills: List[Skill] = []
    seen_names: set = set()

    # 1. Repo-local skills (higher priority — can shadow global skills)
    base = Path(repo_root) / SKILL_DIR_NAME
    if base.is_dir():
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            skill = _parse_skill_file(child)
            if skill is not None:
                skills.append(skill)
                seen_names.add(skill.name)

    # 2. Global skills from ~/.claude/skills/ (lower priority — only added if name not already present)
    global_base = Path.home() / ".claude" / "skills"
    if global_base.is_dir():
        for child in sorted(global_base.iterdir()):
            if not child.is_dir():
                continue
            skill = _parse_skill_file(child)
            if skill is not None and skill.name not in seen_names:
                skills.append(skill)
                seen_names.add(skill.name)

    return skills


def render_skill_manifest(skills: List[Skill]) -> str:
    """Render the short name+description listing for the prompt prefix."""
    if not skills:
        return ""
    lines = [
        "<skills>",
        "IMPORTANT: skill names are NOT callable tools.",
        "To use a skill you MUST first call load_skill(name=\"<skill-name>\") to",
        "retrieve its instructions, then follow those instructions.",
        "Never call a skill name directly as a tool — it will always fail.",
        "",
        "Available skills:",
    ]
    for s in skills:
        lines.append(s.manifest())
    lines.append("</skills>")
    return "\n".join(lines)


def load_skill_body(skills: List[Skill], name: str) -> str:
    """Return the full body + asset manifest for a named skill."""
    for s in skills:
        if s.name == name:
            parts = [f"# Skill: {s.name}", "", s.body]
            if s.argument_hint:
                parts.append("")
                parts.append(f"Argument hint: {s.argument_hint}")
            if s.allowed_tools:
                parts.append("")
                parts.append(
                    "Allowed tools for this skill: " + ", ".join(s.allowed_tools)
                )
            if s.assets:
                parts.append("")
                parts.append("Assets in this skill directory:")
                for asset in s.assets:
                    parts.append(f"- {asset}")
            return "\n".join(parts)
    return f"error: skill '{name}' not found"


def load_skill_body_with_args(skills: List[Skill], name: str, args: Dict[str, str]) -> str:
    """Return skill body with {{key}} substitution."""
    body = load_skill_body(skills, name)
    if body.startswith("error:"):
        return body
    for key, value in args.items():
        body = body.replace(f"{{{{{key}}}}}", str(value))
    return body


def get_skill_commands(skills: List[Skill]) -> List[dict]:
    """Return slash commands derived from skills.

    Each skill with an ``argument_hint`` can expose a /{skill-name} command.
    """
    commands = []
    for s in skills:
        commands.append({
            "name": s.name,
            "description": s.description,
            "effort": s.effort,
        })
    return commands
