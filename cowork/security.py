"""RBAC + tenant isolation primitives.

Stand-in for the planned Postgres RLS + Casbin policies. Provides:

* ``Actor`` — caller identity (user_id, tenant_id, role).
* ``Resource`` — anything that exposes a ``tenant_id`` field.
* ``Policy`` — role × action -> bool table, with sensible defaults for the
  four roles defined in :mod:`cowork.models`.
* ``require(actor, action, resource=None)`` — raises ``PermissionDenied`` on
  RBAC or tenant mismatch.
* ``audit(store, actor, action, target, status, ...)`` — convenience that
  appends an entry via :class:`cowork.store.Store` if one is provided.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .models import ROLE_ADMIN, ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass
class Actor:
    user_id: str
    tenant_id: str
    role: str

    def is_role(self, *roles: str) -> bool:
        return self.role in roles


class HasTenant(Protocol):
    tenant_id: str


class PermissionDenied(Exception):
    pass


# ---------------------------------------------------------------------------
# Action constants (free-form strings; constants here for safety)
# ---------------------------------------------------------------------------

ACTION_READ = "read"
ACTION_WRITE = "write"
ACTION_DELETE = "delete"
ACTION_INVITE = "invite"
ACTION_MANAGE_BILLING = "manage_billing"
ACTION_MANAGE_MEMBERS = "manage_members"
ACTION_EXECUTE_TOOL = "execute_tool"
ACTION_EXPORT = "export"


_DEFAULT_MATRIX: dict[str, set[str]] = {
    ROLE_OWNER: {
        ACTION_READ, ACTION_WRITE, ACTION_DELETE, ACTION_INVITE,
        ACTION_MANAGE_BILLING, ACTION_MANAGE_MEMBERS, ACTION_EXECUTE_TOOL,
        ACTION_EXPORT,
    },
    ROLE_ADMIN: {
        ACTION_READ, ACTION_WRITE, ACTION_DELETE, ACTION_INVITE,
        ACTION_MANAGE_MEMBERS, ACTION_EXECUTE_TOOL, ACTION_EXPORT,
    },
    ROLE_EDITOR: {
        ACTION_READ, ACTION_WRITE, ACTION_EXECUTE_TOOL, ACTION_EXPORT,
    },
    ROLE_VIEWER: {ACTION_READ},
}


class Policy:
    """Mutable role × action permission matrix."""

    def __init__(self, matrix: Optional[dict[str, set[str]]] = None):
        # Deep-ish copy.
        src = matrix if matrix is not None else _DEFAULT_MATRIX
        self._matrix = {role: set(actions) for role, actions in src.items()}

    def allow(self, role: str, action: str) -> None:
        self._matrix.setdefault(role, set()).add(action)

    def deny(self, role: str, action: str) -> None:
        self._matrix.get(role, set()).discard(action)

    def is_allowed(self, role: str, action: str) -> bool:
        return action in self._matrix.get(role, set())


DEFAULT_POLICY = Policy()


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

def check_tenant(actor: Actor, resource: Any) -> bool:
    rt = getattr(resource, "tenant_id", None)
    return rt is None or rt == actor.tenant_id


def require(
    actor: Actor,
    action: str,
    resource: Any = None,
    *,
    policy: Policy = DEFAULT_POLICY,
) -> None:
    if not policy.is_allowed(actor.role, action):
        raise PermissionDenied(
            f"role {actor.role!r} not permitted to {action!r}"
        )
    if resource is not None and not check_tenant(actor, resource):
        raise PermissionDenied(
            f"actor tenant {actor.tenant_id!r} cannot access tenant "
            f"{getattr(resource, 'tenant_id', None)!r}"
        )


def filter_by_tenant(actor: Actor, items: list[Any]) -> list[Any]:
    return [it for it in items if check_tenant(actor, it)]


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

def audit(
    store,
    actor: Actor,
    action: str,
    *,
    target: str = "",
    status: str = "ok",
    metadata: Optional[dict] = None,
):
    """Best-effort audit log append. ``store`` may be ``None``.

    Builds an :class:`cowork.models.AuditLog` and appends via ``store``.
    Returns the appended log (or ``None`` if no store given).
    """
    if store is None:
        return None
    import json as _json
    from .models import AuditLog
    md = dict(metadata or {})
    md.setdefault("status", status)
    log = AuditLog(
        tenant_id=actor.tenant_id,
        actor_id=actor.user_id,
        action=action,
        target=target,
        metadata=_json.dumps(md, sort_keys=True),
    )
    return store.append_audit(log)
