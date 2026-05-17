"""Smoke tests for the cowork demo CLI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cowork.cli import main


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_cli_demo_json_smoke(capsys):
    rc = main(["demo", "--workers", "2", "--tasks", "3", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["kanban"]["done"] == 3
    assert "ms_graph" in data["connectors"]
    assert data["audit_count"] >= 1


def test_cli_demo_human(capsys):
    rc = main(["demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cowork demo" in out
    assert "kanban" in out


def test_cli_help_returns_zero(capsys):
    assert main(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out


def test_cli_unknown_command(capsys):
    rc = main(["unknown"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown command" in err


def test_python_dash_m_cowork_runs():
    res = subprocess.run(
        [sys.executable, "-m", "cowork", "demo", "--json"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin:/usr/local/bin"},
    )
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert "kanban" in data
