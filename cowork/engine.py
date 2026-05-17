"""Codelet subprocess engine.

Wraps ``python -m codelet`` as an opaque agent runtime. Cowork never imports
from `codelet`; we communicate via CLI args, subprocess stdin/stdout, and
the shared ``.mini-coding-agent/`` session directory.

The plan's `SessionBridge` is realised here as a thin record mapping
``cowork_session_id -> codelet_session_id``, plus PID and exit metadata.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from . import parser as P


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class CodeletInvocation:
    """Inputs for one codelet subprocess run."""

    prompt: str
    cwd: Path
    approval: str = "auto"  # auto | ask | never
    resume_session_id: Optional[str] = None
    config_path: Optional[Path] = None
    model: Optional[str] = None
    backend: Optional[str] = None  # ollama | openai
    extra_args: Sequence[str] = field(default_factory=tuple)
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout: Optional[float] = None
    # For testing: override the executable used. Defaults to current python.
    python_executable: str = field(default_factory=lambda: sys.executable)
    # For testing: replace the ``-m codelet`` module spec.
    module_spec: str = "codelet"


@dataclass
class CodeletResult:
    """Outcome of one codelet subprocess run."""

    returncode: int
    stdout: str
    stderr: str
    duration: float
    events: list[P.ParseEvent]
    final: Optional[str]
    pid: Optional[int] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class CodeletEngine:
    """Synchronous codelet subprocess driver."""

    def __init__(self, *, default_approval: str = "auto") -> None:
        self.default_approval = default_approval

    # ---- argv construction --------------------------------------------
    def build_argv(self, inv: CodeletInvocation) -> list[str]:
        argv: list[str] = [
            inv.python_executable,
            "-m",
            inv.module_spec,
            "--no-welcome",
            "--approval",
            inv.approval or self.default_approval,
        ]
        if inv.resume_session_id:
            argv.extend(["--resume", inv.resume_session_id])
        if inv.config_path:
            argv.extend(["--config", str(inv.config_path)])
        if inv.model:
            argv.extend(["--model", inv.model])
        if inv.backend:
            argv.extend(["--backend", inv.backend])
        if inv.extra_args:
            argv.extend(list(inv.extra_args))
        # Trailing prompt: codelet accepts positional prompt args.
        if inv.prompt:
            argv.extend(["-p", inv.prompt])
        return argv

    # ---- run -----------------------------------------------------------
    def run(self, inv: CodeletInvocation) -> CodeletResult:
        import time

        argv = self.build_argv(inv)
        env = os.environ.copy()
        env.update(inv.env_overrides)
        env.setdefault("PYTHONIOENCODING", "utf-8")

        started = time.time()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(inv.cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=inv.timeout,
                text=True,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.time() - started
            return CodeletResult(
                returncode=-1,
                stdout=exc.stdout or "",
                stderr=(exc.stderr or "") + f"\n[cowork] timeout after {inv.timeout}s",
                duration=duration,
                events=[],
                final=None,
            )
        duration = time.time() - started

        events = P.parse_codelet_output(proc.stdout)
        final = P.extract_final(proc.stdout)
        return CodeletResult(
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration=duration,
            events=events,
            final=final,
            pid=None,  # subprocess.run does not expose pid after completion
        )

    # ---- async / streaming --------------------------------------------
    def stream(self, inv: CodeletInvocation, *, on_line=None) -> CodeletResult:
        """Run codelet and emit each stdout line to ``on_line`` as it appears.

        Returns the same CodeletResult as ``run`` but populated incrementally.
        """
        import time

        argv = self.build_argv(inv)
        env = os.environ.copy()
        env.update(inv.env_overrides)
        env.setdefault("PYTHONIOENCODING", "utf-8")

        started = time.time()
        proc = subprocess.Popen(
            argv,
            cwd=str(inv.cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        out_lines: list[str] = []
        err_lines: list[str] = []

        def _drain(stream, sink, callback=None):
            for line in stream:
                sink.append(line)
                if callback is not None:
                    try:
                        callback(line)
                    except Exception:
                        pass

        t_out = threading.Thread(target=_drain, args=(proc.stdout, out_lines, on_line), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, err_lines, None), daemon=True)
        t_out.start()
        t_err.start()
        try:
            proc.wait(timeout=inv.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        duration = time.time() - started

        stdout = "".join(out_lines)
        stderr = "".join(err_lines)
        events = P.parse_codelet_output(stdout)
        final = P.extract_final(stdout)
        return CodeletResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            events=events,
            final=final,
            pid=proc.pid,
        )


# ---------------------------------------------------------------------------
# Session bridge
# ---------------------------------------------------------------------------

@dataclass
class SessionBridge:
    """Maps a cowork session to a codelet session id on disk.

    Codelet stores its sessions under ``<cwd>/.mini-coding-agent/sessions/``.
    The bridge records the latest codelet session id observed for resume.
    """

    cowork_session_id: str
    workspace_cwd: Path
    codelet_session_id: Optional[str] = None
    last_result: Optional[CodeletResult] = None

    @property
    def session_dir(self) -> Path:
        return self.workspace_cwd / ".mini-coding-agent" / "sessions"

    def detect_latest(self) -> Optional[str]:
        """Inspect the session dir and return the most recent session id (filename stem)."""
        d = self.session_dir
        if not d.is_dir():
            return None
        entries = [p for p in d.iterdir() if p.is_file()]
        if not entries:
            return None
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return entries[0].stem
