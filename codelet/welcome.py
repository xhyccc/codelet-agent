"""ASCII welcome banner rendered when the CLI starts."""

import shutil

from .utils import WELCOME_ART, middle


def build_welcome(agent, model, host=None, *, backend="ollama"):
    """Render the boxed welcome banner shown on agent startup."""
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center("Codelet (derived from Mini Code Agent), mieu~"),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BACKEND", backend),
            pair("APPROVAL", agent.approval_policy, "BRANCH", agent.workspace.branch),
            row("SESSION  " + middle(agent.session["id"], inner - 9)),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])
