"""Tests for the cowork SQLite store and tenant isolation."""
from __future__ import annotations

import pytest

from cowork import models as M
from cowork.store import Store


@pytest.fixture()
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_create_and_get_tenant(store):
    t = store.create_tenant(M.Tenant(name="Acme"))
    got = store.get_tenant(t.id)
    assert got is not None
    assert got.name == "Acme"


def test_users_scoped_to_tenant(store):
    t1 = store.create_tenant(M.Tenant(name="t1"))
    t2 = store.create_tenant(M.Tenant(name="t2"))
    store.create_user(M.User(tenant_id=t1.id, email="a@t1"))
    store.create_user(M.User(tenant_id=t1.id, email="b@t1"))
    store.create_user(M.User(tenant_id=t2.id, email="a@t2"))
    assert {u.email for u in store.list_users(t1.id)} == {"a@t1", "b@t1"}
    assert {u.email for u in store.list_users(t2.id)} == {"a@t2"}


def test_workspace_tenant_isolation(store):
    t1 = store.create_tenant(M.Tenant(name="t1"))
    t2 = store.create_tenant(M.Tenant(name="t2"))
    w1 = store.create_workspace(M.Workspace(tenant_id=t1.id, name="w1"))
    # Cross-tenant lookup must return None even with correct workspace id.
    assert store.get_workspace(t2.id, w1.id) is None
    assert store.get_workspace(t1.id, w1.id) is not None


def test_session_and_agent_lifecycle(store):
    t = store.create_tenant(M.Tenant(name="t"))
    w = store.create_workspace(M.Workspace(tenant_id=t.id, name="w"))
    s = store.create_session(M.Session(tenant_id=t.id, workspace_id=w.id, title="ses"))
    a = store.create_agent(M.AgentInstance(session_id=s.id, role="lead"))
    store.update_agent(a.id, codelet_session_id="cs_xyz", status="running", pid=42)
    agents = store.list_agents(s.id)
    assert len(agents) == 1
    assert agents[0].codelet_session_id == "cs_xyz"
    assert agents[0].status == "running"
    assert agents[0].pid == 42


def test_artifact_and_audit(store):
    t = store.create_tenant(M.Tenant(name="t"))
    w = store.create_workspace(M.Workspace(tenant_id=t.id, name="w"))
    s = store.create_session(M.Session(tenant_id=t.id, workspace_id=w.id))
    store.create_artifact(M.Artifact(session_id=s.id, kind="html", path="a.html"))
    store.create_artifact(M.Artifact(session_id=s.id, kind="markdown", path="b.md"))
    assert {a.kind for a in store.list_artifacts(s.id)} == {"html", "markdown"}
    store.append_audit(M.AuditLog(tenant_id=t.id, actor_id="u1", action="session.create", target=s.id))
    logs = store.list_audit(t.id)
    assert len(logs) == 1
    assert logs[0].action == "session.create"


def test_member_role(store):
    t = store.create_tenant(M.Tenant(name="t"))
    u = store.create_user(M.User(tenant_id=t.id, email="x@y"))
    w = store.create_workspace(M.Workspace(tenant_id=t.id, name="w"))
    store.add_member(M.WorkspaceMember(workspace_id=w.id, user_id=u.id, role=M.ROLE_ADMIN))
    assert store.get_member_role(w.id, u.id) == M.ROLE_ADMIN
    store.add_member(M.WorkspaceMember(workspace_id=w.id, user_id=u.id, role=M.ROLE_VIEWER))
    assert store.get_member_role(w.id, u.id) == M.ROLE_VIEWER
