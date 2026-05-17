"""Tests for cowork.guardrails (F16)."""
import pytest
from cowork.guardrails import (
    assess_sensitivity,
    is_sensitive,
    PayloadWarning,
    DiffReview,
    GuardrailEngine,
    SENSITIVITY_LOW,
    SENSITIVITY_MEDIUM,
    SENSITIVITY_HIGH,
    SENSITIVITY_CRITICAL,
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
)


class TestAssessSensitivity:
    def test_benign_name(self):
        assert assess_sensitivity("meeting_notes.md") == SENSITIVITY_LOW

    def test_single_pattern_medium(self):
        assert assess_sensitivity("HR_onboarding.pdf") == SENSITIVITY_MEDIUM

    def test_two_patterns_high(self):
        # "salary" (financials) + "password" (credentials)
        assert assess_sensitivity("salary_passwords.xlsx") == SENSITIVITY_HIGH

    def test_three_plus_patterns_critical(self):
        # financials + credentials + pii
        result = assess_sensitivity("payroll_ssn_api_key.csv")
        assert result in (SENSITIVITY_HIGH, SENSITIVITY_CRITICAL)

    def test_content_sample_raises_level(self):
        # name is benign but content contains "patient medical record"
        level = assess_sensitivity("report.pdf", content="patient medical record")
        assert level != SENSITIVITY_LOW

    def test_is_sensitive_true(self):
        assert is_sensitive("HR_financials.xlsx") is True

    def test_is_sensitive_false(self):
        assert is_sensitive("project_readme.md") is False


class TestPayloadWarning:
    def test_auto_message_generated(self):
        w = PayloadWarning(
            resource_path="/data/HR_payroll.xlsx",
            size_bytes=1024,
            sensitivity=SENSITIVITY_HIGH,
            workspace_visibility="private",
        )
        assert "HR_payroll.xlsx" in w.message
        assert "HIGH" in w.message

    def test_public_workspace_warning_in_message(self):
        w = PayloadWarning(
            resource_path="/data/HR_financials.xlsx",
            size_bytes=512,
            sensitivity=SENSITIVITY_HIGH,
            workspace_visibility="org",
        )
        assert "org" in w.message or "⚠" in w.message

    def test_to_dict_keys(self):
        w = PayloadWarning("path", 100, SENSITIVITY_LOW, "private")
        d = w.to_dict()
        assert all(k in d for k in ("id", "resource_path", "size_bytes",
                                    "sensitivity", "workspace_visibility", "message"))


class TestDiffReview:
    def test_unified_diff_shows_change(self):
        dr = DiffReview(
            file_path="app.py",
            action="modify",
            before="x = 1\n",
            after="x = 99\n",
        )
        diff = dr.unified_diff()
        assert "-x = 1" in diff
        assert "+x = 99" in diff

    def test_approve(self):
        dr = DiffReview(file_path="f.py", action="modify")
        dr.approve(feedback="LGTM")
        assert dr.status == STATUS_APPROVED
        assert dr.feedback == "LGTM"
        assert dr.resolved_at is not None

    def test_reject(self):
        dr = DiffReview(file_path="f.py", action="delete")
        dr.reject(feedback="Too risky")
        assert dr.status == STATUS_REJECTED

    def test_to_dict_includes_diff(self):
        dr = DiffReview(file_path="f.py", action="create",
                        before="", after="print('hi')\n")
        d = dr.to_dict()
        assert "diff" in d
        assert d["status"] == STATUS_PENDING


class TestGuardrailEngine:
    def test_safe_resource_returns_none(self):
        eng = GuardrailEngine()
        w = eng.check_payload("readme.md", size_bytes=200, workspace_visibility="private")
        assert w is None

    def test_high_sensitivity_returns_warning(self):
        eng = GuardrailEngine()
        w = eng.check_payload("HR_salary_passwords.xlsx", size_bytes=1024,
                               workspace_visibility="private")
        assert w is not None
        assert w.sensitivity in (SENSITIVITY_HIGH, SENSITIVITY_CRITICAL)

    def test_oversized_resource_triggers_warning(self):
        eng = GuardrailEngine()
        big = GuardrailEngine.MAX_INGEST_BYTES + 1
        w = eng.check_payload("archive.zip", size_bytes=big)
        assert w is not None

    def test_medium_in_public_workspace_triggers_warning(self):
        eng = GuardrailEngine()
        # single HR pattern → medium; public workspace → warn
        w = eng.check_payload("HR_onboarding.pdf", size_bytes=100,
                               workspace_visibility="org")
        assert w is not None

    def test_dismiss_warning(self):
        eng = GuardrailEngine()
        w = eng.check_payload("HR_payroll.xlsx", size_bytes=1024)
        assert w is not None
        assert eng.dismiss_warning(w.id) is True
        assert len(eng.list_warnings()) == 0

    def test_request_and_resolve_diff_approve(self):
        eng = GuardrailEngine()
        dr = eng.request_diff_review(
            DiffReview(file_path="main.py", action="modify",
                       before="a = 1\n", after="a = 2\n")
        )
        assert len(eng.list_pending_diffs()) == 1
        resolved = eng.resolve_diff(dr.id, approved=True, feedback="ok")
        assert resolved.status == STATUS_APPROVED
        assert len(eng.list_pending_diffs()) == 0

    def test_request_and_resolve_diff_reject(self):
        eng = GuardrailEngine()
        dr = eng.request_diff_review(
            DiffReview(file_path="delete_all.sh", action="execute")
        )
        resolved = eng.resolve_diff(dr.id, approved=False, feedback="No!")
        assert resolved.status == STATUS_REJECTED

    def test_resolve_unknown_diff_returns_none(self):
        eng = GuardrailEngine()
        assert eng.resolve_diff("bad_id", approved=True) is None

    def test_list_diffs_includes_resolved(self):
        eng = GuardrailEngine()
        dr = eng.request_diff_review(DiffReview(file_path="f", action="modify"))
        eng.resolve_diff(dr.id, approved=True)
        all_diffs = eng.list_diffs()
        assert len(all_diffs) == 1
        assert all_diffs[0].status == STATUS_APPROVED
