"""Tests for section_shared_files_warning (cycle 1, cross-provider impact warning)."""

import json
import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def mock_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_history (ticket_id TEXT PRIMARY KEY, files_changed TEXT)")
    return conn


@pytest.fixture
def fake_shared_files():
    """Minimal shared_files list mimicking conventions.yaml structure."""
    return [
        {
            "path_pattern": "express-api-v1/src/routes/payouts/**",
            "used_by": ["paysafe", "trustly", "payper"],
            "change_risk": "validation changes affect ALL listed providers",
            "check": "run provider_type_map for 2+ other providers",
        },
        {
            "path_pattern": "grpc-apm-*/methods/payout.js",
            "used_by": ["all_apm_providers_payout_method"],
            "convention": "paymentMethod threaded from req, never hardcoded",
            "check": "reference: grpc-providers-paysafe/methods/payout.js",
        },
        {
            "path_pattern": "workflow-provider-webhooks/activities/*/payment/handle-activities.js",
            "used_by": ["per_provider_webhook_handler"],
            "convention": "tx_action switch must cover sale|refund|payout",
            "check": "read 1 sibling provider's handle-activities.js",
        },
    ]


def _make_ctx(conn, description: str, provider: str = ""):
    from src.tools.analyze.base import AnalysisContext

    return AnalysisContext(
        conn=conn,
        description=description,
        words=set(description.lower().split()),
        provider=provider,
    )


class TestSharedFilesWarning:
    def test_empty_shared_files_returns_empty(self, mock_conn):
        """If SHARED_FILES constant is empty, section returns empty string."""
        from src.tools.analyze.shared_sections import section_shared_files_warning

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", []):
            ctx = _make_ctx(mock_conn, "review PI-60 payper payout")
            result = section_shared_files_warning(ctx)
            assert result == ""

    def test_review_mode_keyword_triggers_reminder(self, mock_conn, fake_shared_files):
        """Description with review keywords emits REVIEW MODE section even without task history."""
        from src.tools.analyze.shared_sections import section_shared_files_warning

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", fake_shared_files):
            ctx = _make_ctx(mock_conn, "review the changes I made for payper payout", provider="payper")
            result = section_shared_files_warning(ctx)
            assert "REVIEW MODE" in result
            assert "git diff" in result
            assert "provider_type_map" in result

    def test_ukrainian_review_keywords_trigger_reminder(self, mock_conn, fake_shared_files):
        """Ukrainian review keywords (глянь, зламали, досліди) also trigger review mode."""
        from src.tools.analyze.shared_sections import section_shared_files_warning

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", fake_shared_files):
            ctx = _make_ctx(
                mock_conn,
                "глянь зміни pi_60 payout чи нічого не зламали",
                provider="payper",
            )
            result = section_shared_files_warning(ctx)
            assert "REVIEW MODE" in result

    def test_non_review_task_no_reminder(self, mock_conn, fake_shared_files):
        """A plain 'add feature' task without review keywords does NOT emit REVIEW MODE."""
        from src.tools.analyze.shared_sections import section_shared_files_warning

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", fake_shared_files):
            ctx = _make_ctx(mock_conn, "implement a new payout feature for payper", provider="payper")
            result = section_shared_files_warning(ctx)
            assert "REVIEW MODE" not in result

    def test_task_history_match_emits_specific_warning(self, mock_conn, fake_shared_files):
        """If task_id is in task_history and files_changed match shared_files, emit specific warnings."""
        from src.tools.analyze.shared_sections import section_shared_files_warning

        files = [
            "grpc-apm-payper/methods/payout.js",
            "workflow-provider-webhooks/activities/payper/payment/handle-activities.js",
            "grpc-apm-payper/package.json",
        ]
        mock_conn.execute(
            "INSERT INTO task_history (ticket_id, files_changed) VALUES (?, ?)",
            ("PI-60", json.dumps(files)),
        )

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", fake_shared_files):
            ctx = _make_ctx(mock_conn, "audit PI-60 payper changes", provider="payper")
            result = section_shared_files_warning(ctx)
            assert "SHARED FILE IMPACT" in result
            assert "grpc-apm-payper/methods/payout.js" in result
            assert "handle-activities.js" in result
            # package.json should NOT be flagged (not in shared_files)
            assert "package.json" not in result.split("SHARED FILE IMPACT")[1]

    def test_sibling_tool_calls_emitted_for_concrete_providers(self, mock_conn, fake_shared_files):
        """used_by with real provider names → emit provider_type_map call for first sibling."""
        from src.tools.analyze.shared_sections import section_shared_files_warning

        files = ["express-api-v1/src/routes/payouts/payouts.js"]
        mock_conn.execute(
            "INSERT INTO task_history (ticket_id, files_changed) VALUES (?, ?)",
            ("PI-60", json.dumps(files)),
        )

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", fake_shared_files):
            ctx = _make_ctx(mock_conn, "review PI-60", provider="payper")
            result = section_shared_files_warning(ctx)
            # payper excluded, paysafe should be primary sibling
            assert 'provider_type_map("paysafe"' in result
            assert 'provider_type_map("payper"' not in result

    def test_sibling_tool_calls_use_fallback_for_semantic_markers(self, mock_conn, fake_shared_files):
        """used_by with semantic markers (all_apm_providers_*) → fall back to hardcoded APM list."""
        from src.tools.analyze.shared_sections import section_shared_files_warning

        files = ["grpc-apm-payper/methods/payout.js"]
        mock_conn.execute(
            "INSERT INTO task_history (ticket_id, files_changed) VALUES (?, ?)",
            ("PI-60", json.dumps(files)),
        )

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", fake_shared_files):
            ctx = _make_ctx(mock_conn, "review PI-60", provider="payper")
            result = section_shared_files_warning(ctx)
            # Should still emit provider_type_map with a real provider name (paysafe is first fallback)
            assert 'provider_type_map("paysafe"' in result
            # Method should be inferred from file name
            assert '"payout"' in result

    def test_exclude_task_id_skips_own_task_in_blind_eval(self, mock_conn, fake_shared_files):
        """exclude_task_id (LOO mode) must skip task_history lookup for the excluded task."""
        from src.tools.analyze.base import AnalysisContext
        from src.tools.analyze.shared_sections import section_shared_files_warning

        files = ["grpc-apm-payper/methods/payout.js"]
        mock_conn.execute(
            "INSERT INTO task_history (ticket_id, files_changed) VALUES (?, ?)",
            ("PI-60", json.dumps(files)),
        )

        with patch("src.tools.analyze.shared_sections.SHARED_FILES", fake_shared_files):
            ctx = AnalysisContext(
                conn=mock_conn,
                description="analyze PI-60 payper",  # no review keyword
                words={"analyze", "pi-60", "payper"},
                provider="payper",
                exclude_task_id="PI-60",
            )
            result = section_shared_files_warning(ctx)
            # Should not show SHARED FILE IMPACT because task is excluded
            assert "SHARED FILE IMPACT" not in result
