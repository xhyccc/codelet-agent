"""Tests for cowork.admin (F15)."""
import pytest
from cowork.admin import (
    AdminStore,
    SSOConfig,
    RBACRole,
    SCIMGroup,
    TelemetryEvent,
    ALL_CAPABILITIES,
    CAPABILITY_CHAT,
    CAPABILITY_ADMIN,
    CAPABILITY_BILLING,
    CAPABILITY_API_KEYS,
)


@pytest.fixture()
def store():
    return AdminStore(":memory:")


class TestSSO:
    def test_save_and_get(self, store):
        cfg = SSOConfig(
            provider="saml",
            metadata_url="https://idp.example.com/metadata",
            client_id="abc",
            require_sso=True,
            allowed_domains=["example.com"],
        )
        store.save_sso(cfg)
        got = store.get_sso()
        assert got is not None
        assert got.provider == "saml"
        assert got.allowed_domains == ["example.com"]

    def test_get_returns_none_when_not_set(self):
        fresh = AdminStore(":memory:")
        assert fresh.get_sso() is None

    def test_require_sso_flag(self, store):
        store.save_sso(SSOConfig(provider="oidc", require_sso=False))
        assert store.get_sso().require_sso is False


class TestRBAC:
    def test_default_roles_seeded(self, store):
        names = {r.name for r in store.list_roles()}
        assert "workspace_user" in names
        assert "workspace_admin" in names

    def test_workspace_user_has_only_chat(self, store):
        role = store.get_role("workspace_user")
        assert CAPABILITY_CHAT in role.capabilities
        assert CAPABILITY_ADMIN not in role.capabilities

    def test_workspace_admin_has_admin_cap(self, store):
        assert store.check_capability("workspace_admin", CAPABILITY_ADMIN)

    def test_workspace_developer_lacks_billing(self, store):
        assert not store.check_capability("workspace_developer", CAPABILITY_BILLING)

    def test_upsert_custom_role(self, store):
        custom = RBACRole(
            name="read_only_analyst",
            capabilities={CAPABILITY_CHAT, CAPABILITY_API_KEYS},
            is_custom=True,
        )
        store.upsert_role(custom)
        got = store.get_role("read_only_analyst")
        assert got is not None
        assert got.is_custom is True
        assert CAPABILITY_CHAT in got.capabilities

    def test_check_capability_unknown_role(self, store):
        assert store.check_capability("no_such_role", CAPABILITY_CHAT) is False

    def test_all_capabilities_constant_non_empty(self):
        assert len(ALL_CAPABILITIES) >= 8


class TestSCIM:
    def test_sync_and_list(self, store):
        g = SCIMGroup(external_id="g1", name="Engineering", mapped_role="workspace_developer")
        store.sync_group(g)
        groups = store.list_groups()
        assert any(grp.external_id == "g1" for grp in groups)

    def test_update_on_re_sync(self, store):
        g = SCIMGroup(external_id="g2", name="Finance", mapped_role="workspace_user")
        store.sync_group(g)
        g.mapped_role = "workspace_limited_developer"
        store.sync_group(g)
        found = next(x for x in store.list_groups() if x.external_id == "g2")
        assert found.mapped_role == "workspace_limited_developer"


class TestTelemetry:
    def _evt(self, session="s1", event_type="session_start", tokens=100, dur=500):
        return TelemetryEvent(
            session_id=session, event_type=event_type,
            tokens_used=tokens, duration_ms=dur,
        )

    def test_record_and_aggregate(self, store):
        store.record(self._evt(tokens=200))
        store.record(self._evt(tokens=300, event_type="tool_use"))
        agg = store.aggregate_usage(days=30)
        assert agg["total_tokens"] == 500
        assert agg["event_count"] == 2

    def test_event_type_breakdown(self, store):
        store.record(self._evt(event_type="session_start"))
        store.record(self._evt(event_type="tool_use"))
        store.record(self._evt(event_type="tool_use"))
        bd = store.event_type_breakdown(days=30)
        assert bd.get("tool_use") == 2

    def test_export_csv_headers(self, store):
        store.record(self._evt())
        csv_text = store.export_audit_csv(days=180)
        first_line = csv_text.splitlines()[0]
        assert "event_id" in first_line
        assert "tokens_used" in first_line

    def test_export_csv_excludes_prompt_content(self, store):
        """Prompt/response text must never appear in the compliance export."""
        store.record(self._evt())
        csv_text = store.export_audit_csv(days=180)
        assert "prompt" not in csv_text.lower()
        assert "response" not in csv_text.lower()
