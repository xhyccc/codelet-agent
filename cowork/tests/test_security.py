"""Tests for cowork.security (RBAC + tenant isolation + audit)."""
from __future__ import annotations

import json

import pytest

from cowork.models import ROLE_ADMIN, ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER, Tenant, Workspace
from cowork.security import (
    ACTION_DELETE,
    ACTION_INVITE,
    ACTION_MANAGE_BILLING,
    ACTION_READ,
    ACTION_WRITE,
    Actor,
    PermissionDenied,
    Policy,
    audit,
    check_tenant,
    filter_by_tenant,
    require,
)
from cowork.store import Store


# ---------------------------------------------------------------------------
# Policy matrix
# ---------------------------------------------------------------------------

def test_owner_can_manage_billing():
    p = Policy()
    assert p.is_allowed(ROLE_OWNER, ACTION_MANAGE_BILLING)


def test_admin_cannot_manage_billing():
    assert not Policy().is_allowed(ROLE_ADMIN, ACTION_MANAGE_BILLING)


def test_editor_cannot_delete():
    assert not Policy().is_allowed(ROLE_EDITOR, ACTION_DELETE)


def test_viewer_only_reads():
    p = Policy()
    assert p.is_allowed(ROLE_VIEWER, ACTION_READ)
    assert not p.is_allowed(ROLE_VIEWER, ACTION_WRITE)
    assert not p.is_allowed(ROLE_VIEWER, ACTION_INVITE)


def test_policy_allow_deny_mutates():
    p = Policy()
    p.allow(ROLE_VIEWER, ACTION_WRITE)
    assert p.is_allowed(ROLE_VIEWER, ACTION_WRITE)
    p.deny(ROLE_VIEWER, ACTION_WRITE)
    assert not p.is_allowed(ROLE_VIEWER, ACTION_WRITE)


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

def test_require_allows_same_tenant():
    actor = Actor(user_id="u1", tenant_id="t1", role=ROLE_EDITOR)
    ws = Workspace(tenant_id="t1", name="x")
    require(actor, ACTION_WRITE, ws)


def test_require_blocks_cross_tenant():
    actor = Actor(user_id="u1", tenant_id="t1", role=ROLE_ADMIN)
    ws = Workspace(tenant_id="t2", name="x")
    with pytest.raises(PermissionDenied):
        require(actor, ACTION_READ, ws)


def test_require_blocks_action_not_in_policy():
    actor = Actor(user_id="u1", tenant_id="t1", role=ROLE_VIEWER)
    ws = Workspace(tenant_id="t1", name="x")
    with pytest.raises(PermissionDenied):
        require(actor, ACTION_WRITE, ws)


def test_check_tenant_passes_when_resource_has_no_tenant():
    actor = Actor(user_id="u1", tenant_id="t1", role=ROLE_OWNER)
    assert check_tenant(actor, object())


def test_filter_by_tenant_removes_foreign():
    actor = Actor(user_id="u1", tenant_id="t1", role=ROLE_OWNER)
    items = [Workspace(tenant_id="t1", name="a"), Workspace(tenant_id="t2", name="b")]
    out = filter_by_tenant(actor, items)
    assert len(out) == 1 and out[0].name == "a"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_audit_writes_to_store(tmp_path):
    store = Store(db_path=str(tmp_path / "x.db"))
    tenant = store.create_tenant(Tenant(name="t"))
    actor = Actor(user_id="u1", tenant_id=tenant.id, role=ROLE_OWNER)
    audit(store, actor, "workspace.create", target="ws_1", metadata={"name": "X"})
    logs = store.list_audit(tenant.id)
    assert len(logs) == 1
    log = logs[0]
    assert log.action == "workspace.create"
    assert log.actor_id == "u1"
    md = json.loads(log.metadata)
    assert md["status"] == "ok"
    assert md["name"] == "X"


def test_audit_with_none_store_is_noop():
    actor = Actor(user_id="u", tenant_id="t", role=ROLE_OWNER)
    assert audit(None, actor, "noop") is None
