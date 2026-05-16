"""Lightweight sandboxing for risky tools.

This is best-effort defense in depth, not a true security boundary. The
denylists are intentionally conservative: catch obvious destructive or
privilege-escalating patterns without trying to be a real sandbox.

The denylists themselves can be overridden via YAML config in
``codelet/config/default.yaml`` (see ``sandbox`` section).
"""

import os
import re


# Default pattern denylist for shell commands. Each entry is a regex applied
# (case-insensitive) to the whole command string. These can be overridden via
# the YAML harness config under ``sandbox.shell_deny``.
DEFAULT_SHELL_DENY_PATTERNS = (
    r"\bsudo\b",
    r"\bsu\s+-",
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f?|--recursive)[^\n]*\s(/|~|\$HOME)\b",
    r"\brm\s+-rf?\s+/(?:\s|$)",
    r":\s*\(\s*\)\s*\{.*\|\s*:&\s*\}\s*;",  # classic fork bomb :(){ :|:& };:
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\binit\s+0\b",
    r"\binit\s+6\b",
    r"\bchmod\s+(-R\s+)?[0-7]*777[^\n]*\s(/|~|\$HOME)(\s|$)",
    r"\bchown\s+(-R\s+)?[^\n]*\s(/|~|\$HOME)(\s|$)",
    r"\b(curl|wget|fetch)\b[^\n]*\|\s*(sh|bash|zsh|ksh|dash|python[0-9.]*|perl|node|ruby)\b",
    r">\s*/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|hd[a-z]+)\b",
)

# Default pattern denylist for Python code passed to ``run_python``.
DEFAULT_PYTHON_DENY_PATTERNS = (
    r"\bos\.system\s*\(\s*['\"][^'\"\n]*\b(sudo|rm\s+-rf?\s+/|mkfs|shutdown|reboot|halt|poweroff)\b",
    r"\bsubprocess\.[A-Za-z_]+\s*\([^)]*\b(sudo|mkfs|shutdown|reboot|halt|poweroff)\b",
    r"\bshutil\.rmtree\s*\(\s*['\"]/['\"]?\s*\)",
    r"\bshutil\.rmtree\s*\(\s*['\"]/(bin|boot|dev|etc|home|lib|opt|root|sbin|sys|usr|var)\b",
    r"\bopen\s*\(\s*['\"]/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|hd[a-z]+)",
)

# Environment variable name patterns to strip from sandboxed subprocess
# environments to reduce the blast radius of an accidental credential
# exfiltration.
DEFAULT_ENV_STRIP_PATTERNS = (
    re.compile(r"_API_KEY$"),
    re.compile(r"_TOKEN$"),
    re.compile(r"_SECRET$"),
    re.compile(r"_PASSWORD$"),
    re.compile(r"^AWS_"),
    re.compile(r"^AZURE_"),
    re.compile(r"^GCP_"),
    re.compile(r"^GITHUB_TOKEN$"),
    re.compile(r"^OPENAI_API_KEY$"),
    re.compile(r"^ANTHROPIC_API_KEY$"),
)

# Default resource limits for sandboxed subprocesses (POSIX only).
DEFAULT_LIMITS = {
    "cpu_seconds": 30,
    "address_space_bytes": 1024 * 1024 * 1024,
    "file_size_bytes": 64 * 1024 * 1024,
}


# Module-level mutable holders that can be reconfigured by ``apply_config``.
# Tests and the agent runtime read through the ``sandbox_check_*`` helpers
# below, which always consult the current values.
_SHELL_DENY_PATTERNS = tuple(DEFAULT_SHELL_DENY_PATTERNS)
_PYTHON_DENY_PATTERNS = tuple(DEFAULT_PYTHON_DENY_PATTERNS)
_ENV_STRIP_PATTERNS = tuple(DEFAULT_ENV_STRIP_PATTERNS)
_LIMITS = dict(DEFAULT_LIMITS)


# Aliases kept for backward compatibility with the original module-level names.
SANDBOX_SHELL_DENY_PATTERNS = _SHELL_DENY_PATTERNS
SANDBOX_PYTHON_DENY_PATTERNS = _PYTHON_DENY_PATTERNS
SANDBOX_ENV_STRIP_PATTERNS = _ENV_STRIP_PATTERNS
SANDBOX_DEFAULT_LIMITS = _LIMITS


def apply_config(sandbox_cfg):
    """Apply a sandbox section from a loaded YAML config.

    The config is a dict with optional keys ``shell_deny``, ``python_deny``,
    ``env_strip``, ``limits``. Missing keys keep the existing values.
    """
    global _SHELL_DENY_PATTERNS, _PYTHON_DENY_PATTERNS, _ENV_STRIP_PATTERNS, _LIMITS
    global SANDBOX_SHELL_DENY_PATTERNS, SANDBOX_PYTHON_DENY_PATTERNS, SANDBOX_ENV_STRIP_PATTERNS, SANDBOX_DEFAULT_LIMITS
    if not sandbox_cfg:
        return
    if "shell_deny" in sandbox_cfg:
        _SHELL_DENY_PATTERNS = tuple(sandbox_cfg["shell_deny"])
        SANDBOX_SHELL_DENY_PATTERNS = _SHELL_DENY_PATTERNS
    if "python_deny" in sandbox_cfg:
        _PYTHON_DENY_PATTERNS = tuple(sandbox_cfg["python_deny"])
        SANDBOX_PYTHON_DENY_PATTERNS = _PYTHON_DENY_PATTERNS
    if "env_strip" in sandbox_cfg:
        _ENV_STRIP_PATTERNS = tuple(re.compile(p) for p in sandbox_cfg["env_strip"])
        SANDBOX_ENV_STRIP_PATTERNS = _ENV_STRIP_PATTERNS
    if "limits" in sandbox_cfg:
        _LIMITS = dict(sandbox_cfg["limits"])
        SANDBOX_DEFAULT_LIMITS = _LIMITS


def sandbox_check_shell(command):
    """Return an error message if `command` matches any denylisted pattern."""
    for pattern in _SHELL_DENY_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"sandbox: shell command blocked by safety policy (matched: {pattern})"
    return None


def sandbox_check_python(code):
    """Return an error message if `code` matches any denylisted pattern."""
    for pattern in _PYTHON_DENY_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return f"sandbox: python code blocked by safety policy (matched: {pattern})"
    return None


def sandbox_filter_env(env):
    """Return a copy of `env` with obviously sensitive variables removed."""
    filtered = {}
    for key, value in env.items():
        if any(pattern.search(key) for pattern in _ENV_STRIP_PATTERNS):
            continue
        filtered[key] = value
    # Don't let subprocesses inherit a writable PYTHONPATH that could be used
    # to shadow stdlib modules with attacker-controlled code.
    filtered.pop("PYTHONPATH", None)
    return filtered


def sandbox_preexec(limits=None):
    """Return a preexec_fn that applies POSIX resource limits, or None."""
    if os.name != "posix":
        return None
    try:
        import resource  # noqa: F401  (imported lazily; POSIX only)
    except ImportError:
        return None

    limits = limits or _LIMITS

    def _apply():
        import resource as _resource
        try:
            cpu = limits.get("cpu_seconds")
            if cpu:
                _resource.setrlimit(_resource.RLIMIT_CPU, (cpu, cpu))
            mem = limits.get("address_space_bytes")
            if mem and hasattr(_resource, "RLIMIT_AS"):
                _resource.setrlimit(_resource.RLIMIT_AS, (mem, mem))
            fsize = limits.get("file_size_bytes")
            if fsize:
                _resource.setrlimit(_resource.RLIMIT_FSIZE, (fsize, fsize))
            nproc = limits.get("max_processes")
            if nproc and hasattr(_resource, "RLIMIT_NPROC"):
                _resource.setrlimit(_resource.RLIMIT_NPROC, (nproc, nproc))
        except (ValueError, OSError):
            pass

    return _apply
