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
        # Trailing prompt: positional arg — must come LAST, no -p flag.
        if inv.prompt:
            argv.append(inv.prompt)
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

        Lines are delivered to ``on_line`` from the calling thread so that a
        slow or blocking callback cannot leak background IO threads.  Stdout is
        read one character at a time to avoid buffering long lines.

        Returns the same CodeletResult as ``run`` but populated incrementally.
        """
        import queue
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

        # Lines from stdout are put here so on_line runs in the main thread,
        # not in the IO drain thread.  None is the sentinel for end-of-stream.
        _line_q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _drain_stdout(stream, sink):
            buf = ""
            while True:
                ch = stream.read(1)  # read one char to avoid buffering long lines
                if not ch:
                    break
                buf += ch
                if ch == "\n":
                    sink.append(buf)
                    _line_q.put(buf)
                    buf = ""
            if buf:  # last line without a trailing newline
                sink.append(buf)
                _line_q.put(buf)
            _line_q.put(None)  # sentinel: stdout stream closed

        def _drain_stderr(stream, sink):
            for line in stream:
                sink.append(line)

        t_out = threading.Thread(target=_drain_stdout, args=(proc.stdout, out_lines), daemon=True)
        t_err = threading.Thread(target=_drain_stderr, args=(proc.stderr, err_lines), daemon=True)
        t_out.start()
        t_err.start()

        deadline = (started + inv.timeout) if inv.timeout else None
        timed_out = False
        sentinel_seen = False

        while not sentinel_seen:
            now = time.time()
            if deadline is not None and now >= deadline:
                proc.kill()
                try:
                    proc.wait(timeout=5.0)  # bounded wait; avoid blocking on zombie children
                except subprocess.TimeoutExpired:
                    pass
                timed_out = True
                break
            wait_time = max(0.0, min(0.1, deadline - now)) if deadline is not None else 0.1
            try:
                item = _line_q.get(timeout=wait_time)
            except queue.Empty:
                continue
            if item is None:
                sentinel_seen = True
            elif on_line is not None:
                try:
                    on_line(item)
                except Exception:
                    pass

        if not timed_out:
            proc.wait()

        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
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
