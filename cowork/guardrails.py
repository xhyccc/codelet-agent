"""F16 – Human-in-the-loop guardrails: sensitive-payload warnings,
rich diff review, and file-sandbox enforcement (Phase 7).
"""
from __future__ import annotations

import difflib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Sensitivity detection
# ---------------------------------------------------------------------------

_SENSITIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"hr_|human.?resources?",               re.I),
    re.compile(r"financ(e|ial|s)|payroll|salary|salaries|compensation", re.I),
    re.compile(r"password|passwd|secret|credential|api.?key|auth.?token", re.I),
    re.compile(r"pii|personally.?identifiable|ssn|social.?security",    re.I),
    re.compile(r"hipaa|phi|patient|medical.?record",   re.I),
    re.compile(r"legal|attorney|privilege|confidential", re.I),
]

SensitivityLevel = str
SENSITIVITY_LOW = "low"
SENSITIVITY_MEDIUM = "medium"
SENSITIVITY_HIGH = "high"
SENSITIVITY_CRITICAL = "critical"


def assess_sensitivity(name: str, content: str = "") -> SensitivityLevel:
    """Heuristic sensitivity classification based on name and content."""
    text = f"{name} {content}"
    hits = sum(1 for p in _SENSITIVE_PATTERNS if p.search(text))
    if hits == 0:
        return SENSITIVITY_LOW
    if hits == 1:
        return SENSITIVITY_MEDIUM
    if hits == 2:
        return SENSITIVITY_HIGH
    return SENSITIVITY_CRITICAL


def is_sensitive(name: str, content: str = "") -> bool:
    return assess_sensitivity(name, content) in (SENSITIVITY_HIGH, SENSITIVITY_CRITICAL)


# ---------------------------------------------------------------------------
# Payload confirmation warning
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


@dataclass
class PayloadWarning:
    resource_path: str
    size_bytes: int
    sensitivity: SensitivityLevel
    workspace_visibility: str   # private | invited | org
    message: str = ""
    id: str = field(default_factory=lambda: _new_id("pw"))
    at: float = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not self.message:
            self.message = self._build_message()

    def _build_message(self) -> str:
        parts = [
            f"Resource: {self.resource_path}",
            f"Size: {self.size_bytes:,} bytes",
            f"Sensitivity: {self.sensitivity.upper()}",
        ]
        if (self.workspace_visibility != "private"
                and self.sensitivity in (SENSITIVITY_HIGH, SENSITIVITY_CRITICAL)):
            parts.append(
                f"⚠ Sensitive data detected in a {self.workspace_visibility!r} workspace!"
            )
        return " | ".join(parts)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "resource_path": self.resource_path,
            "size_bytes": self.size_bytes,
            "sensitivity": self.sensitivity,
            "workspace_visibility": self.workspace_visibility,
            "message": self.message,
            "at": self.at,
        }


# ---------------------------------------------------------------------------
# Diff review (human-approval gate before file mutations)
# ---------------------------------------------------------------------------

@dataclass
class DiffReview:
    file_path: str
    action: str               # create | modify | delete | execute
    before: str = ""          # empty for "create"
    after: str = ""           # empty for "delete"
    description: str = ""
    status: str = STATUS_PENDING
    feedback: str = ""
    id: str = field(default_factory=lambda: _new_id("diff"))
    created_at: float = field(default_factory=_now)
    resolved_at: Optional[float] = None

    def unified_diff(self, context: int = 3) -> str:
        """Return a unified diff string between before/after."""
        before_lines = self.before.splitlines(keepends=True)
        after_lines = self.after.splitlines(keepends=True)
        return "".join(difflib.unified_diff(
            before_lines, after_lines,
            fromfile=f"a/{self.file_path}",
            tofile=f"b/{self.file_path}",
            n=context,
        ))

    def approve(self, feedback: str = "") -> None:
        self.status = STATUS_APPROVED
        self.feedback = feedback
        self.resolved_at = _now()

    def reject(self, feedback: str = "") -> None:
        self.status = STATUS_REJECTED
        self.feedback = feedback
        self.resolved_at = _now()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "file_path": self.file_path,
            "action": self.action,
            "description": self.description,
            "status": self.status,
            "feedback": self.feedback,
            "diff": self.unified_diff(),
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }


# ---------------------------------------------------------------------------
# GuardrailEngine
# ---------------------------------------------------------------------------

class GuardrailEngine:
    """Thread-safe registry for payload warnings and diff review gates.

    Workflow:
    1. ``check_payload()`` → returns a ``PayloadWarning`` if the resource
       requires user confirmation; ``None`` if safe to proceed immediately.
    2. ``request_diff_review()`` → queues a ``DiffReview`` that must be
       approved/rejected before the agent writes the file.
    3. UI reads ``list_pending_diffs()`` / ``list_warnings()`` and calls
       ``resolve_diff()`` / ``dismiss_warning()`` after human review.
    """

    # Hard cap: warn if ingesting more than 50 MB
    MAX_INGEST_BYTES: int = 50 * 1024 * 1024

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._warnings: dict[str, PayloadWarning] = {}
        self._diffs: dict[str, DiffReview] = {}

    # -- Payload warnings --

    def check_payload(
        self,
        resource_path: str,
        size_bytes: int,
        workspace_visibility: str = "private",
        content_sample: str = "",
    ) -> Optional[PayloadWarning]:
        """Return a warning if the resource needs confirmation, else None."""
        sensitivity = assess_sensitivity(resource_path, content_sample)
        needs_warn = (
            size_bytes > self.MAX_INGEST_BYTES
            or sensitivity in (SENSITIVITY_HIGH, SENSITIVITY_CRITICAL)
            or (workspace_visibility != "private"
                and sensitivity == SENSITIVITY_MEDIUM)
        )
        if not needs_warn:
            return None
        w = PayloadWarning(
            resource_path=resource_path,
            size_bytes=size_bytes,
            sensitivity=sensitivity,
            workspace_visibility=workspace_visibility,
        )
        with self._lock:
            self._warnings[w.id] = w
        return w

    def list_warnings(self) -> list[PayloadWarning]:
        with self._lock:
            return list(self._warnings.values())

    def dismiss_warning(self, warning_id: str) -> bool:
        with self._lock:
            return self._warnings.pop(warning_id, None) is not None

    # -- Diff reviews --

    def request_diff_review(self, diff: DiffReview) -> DiffReview:
        with self._lock:
            self._diffs[diff.id] = diff
        return diff

    def resolve_diff(
        self, diff_id: str, approved: bool, feedback: str = ""
    ) -> Optional[DiffReview]:
        with self._lock:
            d = self._diffs.get(diff_id)
            if d is None:
                return None
            if approved:
                d.approve(feedback)
            else:
                d.reject(feedback)
        return d

    def list_pending_diffs(self) -> list[DiffReview]:
        with self._lock:
            return [d for d in self._diffs.values() if d.status == STATUS_PENDING]

    def list_diffs(self) -> list[DiffReview]:
        with self._lock:
            return list(self._diffs.values())
