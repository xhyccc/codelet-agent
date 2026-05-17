"""Core data models for cowork.

Plain dataclasses (no Pydantic) to stay stdlib-only. The schema mirrors the
plan's PostgreSQL tables; the SQLite store in `cowork.store` provides the
canonical persistence with tenant_id isolation for v1.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Identity / tenancy
# ---------------------------------------------------------------------------

@dataclass
class Tenant:
    name: str
    id: str = field(default_factory=lambda: _new_id("ten"))
    created_at: float = field(default_factory=_now)
    plan: str = "free"  # free | team | enterprise


@dataclass
class User:
    tenant_id: str
    email: str
    display_name: str = ""
    id: str = field(default_factory=lambda: _new_id("usr"))
    created_at: float = field(default_factory=_now)


ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)


@dataclass
class Workspace:
    tenant_id: str
    name: str
    id: str = field(default_factory=lambda: _new_id("ws"))
    created_at: float = field(default_factory=_now)
    description: str = ""


@dataclass
class WorkspaceMember:
    workspace_id: str
    user_id: str
    role: str = ROLE_EDITOR


# ---------------------------------------------------------------------------
# Sessions / agents
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """A cowork session that bridges to one or more codelet sessions."""
    tenant_id: str
    workspace_id: str
    title: str = "session"
    id: str = field(default_factory=lambda: _new_id("ses"))
    created_at: float = field(default_factory=_now)
    status: str = "active"  # active | paused | done | error


@dataclass
class AgentInstance:
    """Mapping cowork session -> codelet child session id + process status."""
    session_id: str
    role: str  # e.g. "lead", "worker", "reviewer"
    codelet_session_id: Optional[str] = None
    id: str = field(default_factory=lambda: _new_id("ag"))
    status: str = "idle"  # idle | running | done | error
    pid: Optional[int] = None
    created_at: float = field(default_factory=_now)


@dataclass
class Artifact:
    session_id: str
    kind: str  # "html" | "react" | "markdown" | "code"
    path: str  # relative path inside the workspace artifact dir
    id: str = field(default_factory=lambda: _new_id("art"))
    created_at: float = field(default_factory=_now)
    title: str = ""


@dataclass
class AuditLog:
    tenant_id: str
    actor_id: str
    action: str  # e.g. "session.create", "agent.spawn", "artifact.write"
    target: str = ""
    id: str = field(default_factory=lambda: _new_id("aud"))
    at: float = field(default_factory=_now)
    metadata: str = ""  # JSON-serialized free-form
