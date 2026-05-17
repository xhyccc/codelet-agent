"""Tests for cowork.projects (F14)."""
import pytest
from cowork.projects import (
    Project,
    ProjectMember,
    ChatSnapshot,
    ProjectStore,
    VISIBILITY_PRIVATE,
    VISIBILITY_ORG,
    ROLE_VIEWER,
    ROLE_EDITOR,
)


@pytest.fixture()
def store():
    return ProjectStore(":memory:")


def _proj(tenant_id="t1", name="Eng", owner="usr_owner", vis=VISIBILITY_PRIVATE):
    return Project(tenant_id=tenant_id, name=name, owner_id=owner, visibility=vis)


class TestProjects:
    def test_create_and_get(self, store):
        p = store.create_project(_proj())
        got = store.get_project(p.id)
        assert got is not None
        assert got.name == "Eng"

    def test_list_returns_for_tenant(self, store):
        store.create_project(_proj(tenant_id="t1", name="P1"))
        store.create_project(_proj(tenant_id="t1", name="P2"))
        store.create_project(_proj(tenant_id="t99", name="Other"))
        projs = store.list_projects("t1")
        assert len(projs) == 2

    def test_update_visibility(self, store):
        p = store.create_project(_proj())
        store.update_project(p.id, visibility=VISIBILITY_ORG)
        assert store.get_project(p.id).visibility == VISIBILITY_ORG

    def test_update_instructions(self, store):
        p = store.create_project(_proj())
        store.update_project(p.id, instructions="Always reply in formal English.")
        assert store.get_project(p.id).instructions == "Always reply in formal English."

    def test_get_missing_returns_none(self, store):
        assert store.get_project("nope") is None


class TestMembers:
    def test_add_and_get_role(self, store):
        p = store.create_project(_proj())
        store.add_member(ProjectMember(project_id=p.id, user_id="u1", role=ROLE_EDITOR))
        assert store.get_member_role(p.id, "u1") == ROLE_EDITOR

    def test_viewer_role(self, store):
        p = store.create_project(_proj())
        store.add_member(ProjectMember(project_id=p.id, user_id="u2", role=ROLE_VIEWER))
        assert store.get_member_role(p.id, "u2") == ROLE_VIEWER

    def test_unknown_member_returns_none(self, store):
        p = store.create_project(_proj())
        assert store.get_member_role(p.id, "ghost") is None

    def test_list_members(self, store):
        p = store.create_project(_proj())
        store.add_member(ProjectMember(project_id=p.id, user_id="u1", role=ROLE_EDITOR))
        store.add_member(ProjectMember(project_id=p.id, user_id="u2", role=ROLE_VIEWER))
        members = store.list_members(p.id)
        assert len(members) == 2

    def test_can_edit_owner(self, store):
        p = store.create_project(_proj(owner="owner_1"))
        assert store.can_edit(p.id, "owner_1") is True

    def test_can_edit_editor_member(self, store):
        p = store.create_project(_proj(owner="owner_1"))
        store.add_member(ProjectMember(project_id=p.id, user_id="editor_u", role=ROLE_EDITOR))
        assert store.can_edit(p.id, "editor_u") is True

    def test_cannot_edit_viewer(self, store):
        p = store.create_project(_proj())
        store.add_member(ProjectMember(project_id=p.id, user_id="viewer_u", role=ROLE_VIEWER))
        assert store.can_edit(p.id, "viewer_u") is False


class TestSnapshots:
    def test_create_and_get(self, store):
        p = store.create_project(_proj())
        snap = store.create_snapshot(
            ChatSnapshot(project_id=p.id, title="Sprint 12",
                         content="[]", creator_id="u1")
        )
        got = store.get_snapshot(snap.id)
        assert got is not None
        assert got.title == "Sprint 12"
        assert got.revoked is False

    def test_revoke_snapshot(self, store):
        p = store.create_project(_proj())
        snap = store.create_snapshot(
            ChatSnapshot(project_id=p.id, title="s", content="[]")
        )
        store.revoke_snapshot(snap.id)
        assert store.get_snapshot(snap.id).revoked is True

    def test_list_snapshots(self, store):
        p = store.create_project(_proj())
        store.create_snapshot(ChatSnapshot(project_id=p.id, title="A", content="[]"))
        store.create_snapshot(ChatSnapshot(project_id=p.id, title="B", content="[]"))
        snaps = store.list_snapshots(p.id)
        assert len(snaps) == 2

    def test_snapshot_to_dict_omits_content(self, store):
        p = store.create_project(_proj())
        snap = store.create_snapshot(
            ChatSnapshot(project_id=p.id, title="T", content="secret chat",
                         artifact_ids=["av1", "av2"])
        )
        d = snap.to_dict()
        assert "content" not in d
        assert d["artifact_count"] == 2
