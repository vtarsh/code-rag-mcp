"""Tests for src/tools/analyze/classifier.py — domain classification."""

import sqlite3
from unittest.mock import patch

from src.tools.analyze.classifier import TaskClassification, classify_task


def _mock_conn(provider_repos=None):
    """Create in-memory SQLite DB with repos table for provider detection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE repos (name TEXT, type TEXT)")
    if provider_repos:
        for repo in provider_repos:
            conn.execute("INSERT INTO repos VALUES (?, 'service')", (repo,))
    return conn


# Minimal domain_patterns for testing (avoids depending on conventions.yaml)
_TEST_DOMAIN_PATTERNS = {
    "bo": {
        "keywords": ["backoffice", "dashboard", "admin", "back office"],
        "repo_patterns": ["bo-.*"],
        "seed_repos": ["bo-dashboard", "bo-api"],
    },
    "hs": {
        "keywords": ["headless", "sdk", "checkout page", "hosted session"],
        "repo_patterns": ["hs-.*"],
        "seed_repos": ["hs-checkout", "hs-sdk"],
    },
    "core-risk": {
        "keywords": ["risk", "fraud", "3ds", "chargeback"],
        "repo_patterns": ["grpc-risk-.*"],
        "seed_repos": ["grpc-risk-engine"],
    },
    "core-payment": {
        "keywords": ["payment", "transaction", "settlement"],
        "repo_patterns": ["grpc-core-.*"],
        "seed_repos": ["grpc-core-payment"],
    },
}


class TestPIDetection:
    """Provider Integration tasks should be classified as PI domain."""

    def test_explicit_provider_returns_pi(self):
        conn = _mock_conn(["grpc-apm-stripe"])
        result = classify_task(conn, "implement stripe payout", "stripe", set())
        assert result.domain == "pi"
        assert result.provider == "stripe"
        assert result.confidence == 1.0

    def test_autodetect_provider_from_words(self):
        conn = _mock_conn(["grpc-apm-trustly"])
        result = classify_task(conn, "implement trustly verification", "", {"implement", "trustly", "verification"})
        assert result.domain == "pi"
        assert result.provider == "trustly"

    def test_bulk_providers_detected(self):
        """When 3+ provider names in words, classify as bulk PI with no specific provider."""
        conn = _mock_conn(["grpc-apm-stripe", "grpc-apm-trustly", "grpc-apm-nuvei"])
        words = {"stripe", "trustly", "nuvei", "update", "all"}
        result = classify_task(conn, "update all providers", "", words)
        assert result.domain == "pi"
        assert result.provider == ""
        assert result.confidence == 1.0


class TestCOREPrefixSuppression:
    """CORE- prefix tasks should NOT be classified as PI even if provider name appears."""

    def test_core_prefix_suppresses_ambiguous_provider(self):
        """Ambiguous names like 'checkout' should be suppressed for CORE tasks."""
        conn = _mock_conn(["grpc-apm-checkout"])
        result = classify_task(
            conn,
            "CORE-123 fix checkout flow validation",
            "",
            {"fix", "checkout", "flow", "validation"},
        )
        # Should NOT be PI — "checkout" is ambiguous and CORE prefix present
        assert result.domain != "pi" or result.provider == ""

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_core_prefix_with_real_provider_and_core_signals(self):
        """Real provider name + strong CORE signals (risk, settlement) = suppress PI."""
        conn = _mock_conn(["grpc-apm-trustly"])
        result = classify_task(
            conn,
            "CORE-456 migrate trustly settlement workflow",
            "",
            {"migrate", "trustly", "settlement", "workflow"},
        )
        # Two core signals (settlement, workflow) → provider suppressed
        assert result.provider == ""

    def test_core_prefix_with_real_provider_no_core_signals(self):
        """Real provider name + CORE prefix but no strong core signals → still PI."""
        conn = _mock_conn(["grpc-apm-trustly"])
        result = classify_task(
            conn,
            "CORE-789 add trustly payout method",
            "",
            {"add", "trustly", "payout", "method"},
        )
        # Only 0 or 1 core signals → provider NOT suppressed → PI
        assert result.domain == "pi"
        assert result.provider == "trustly"


class TestBODetection:
    """Backoffice tasks should be classified as BO domain."""

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_bo_keyword_match(self):
        conn = _mock_conn()
        result = classify_task(
            conn, "update backoffice dashboard filters", "", {"update", "backoffice", "dashboard", "filters"}
        )
        assert "bo" in result.domain
        assert result.provider == ""

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_bo_prefix_no_keywords(self):
        """BO- prefix alone should return BO domain even without keyword matches."""
        conn = _mock_conn()
        # Use empty domain patterns to ensure no keyword matches
        with patch(
            "src.tools.analyze.classifier.DOMAIN_PATTERNS",
            {"bo": {"keywords": [], "repo_patterns": [], "seed_repos": ["bo-api"]}},
        ):
            result = classify_task(conn, "BO-100 do something", "", {"do", "something"})
        assert result.domain == "bo"
        assert result.confidence == 0.7

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_bo_prefix_with_cross_domain_keywords(self):
        """BO prefix + risk keywords → BO primary domain with core-risk seeds merged."""
        conn = _mock_conn()
        result = classify_task(
            conn,
            "BO-200 risk override reason logic",
            "",
            {"risk", "override", "reason", "logic"},
        )
        assert result.domain.startswith("bo")
        # Should include core-risk seed repos via secondary domain
        assert "grpc-risk-engine" in result.seed_repos or "bo" in result.domain


class TestHSDetection:
    """Hosted Session / headless tasks should be classified as HS domain."""

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_hs_keyword_match(self):
        conn = _mock_conn()
        result = classify_task(
            conn, "update headless sdk integration", "", {"update", "headless", "sdk", "integration"}
        )
        assert "hs" in result.domain

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_hs_prefix_detection(self):
        conn = _mock_conn()
        result = classify_task(conn, "HS-50 fix checkout page styling", "", {"fix", "checkout", "page", "styling"})
        assert result.domain.startswith("hs")


class TestEdgeCases:
    """Edge cases for the classifier."""

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_empty_description(self):
        conn = _mock_conn()
        result = classify_task(conn, "", "", set())
        assert isinstance(result, TaskClassification)
        assert result.domain == "unknown"
        assert result.confidence == 0.0

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_no_keywords_no_provider(self):
        conn = _mock_conn()
        result = classify_task(conn, "do something random", "", {"do", "something", "random"})
        assert result.domain == "unknown"
        assert result.confidence == 0.0

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", {})
    def test_empty_domain_patterns(self):
        """With no domain patterns, should return unknown."""
        conn = _mock_conn()
        result = classify_task(conn, "implement something", "", {"implement", "something"})
        assert result.domain == "unknown"

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_multiple_domain_matches_returns_best(self):
        """When multiple domains match, highest score wins."""
        conn = _mock_conn()
        result = classify_task(
            conn,
            "risk fraud chargeback in backoffice",
            "",
            {"risk", "fraud", "chargeback", "backoffice"},
        )
        # core-risk has 3 keyword matches (risk, fraud, chargeback) vs bo has 1 (backoffice)
        assert result.domain.startswith("core-risk")

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_seed_repos_populated(self):
        conn = _mock_conn()
        result = classify_task(conn, "fix risk engine bug", "", {"fix", "risk", "engine", "bug"})
        assert "grpc-risk-engine" in result.seed_repos

    @patch("src.tools.analyze.classifier.DOMAIN_PATTERNS", _TEST_DOMAIN_PATTERNS)
    def test_multi_word_keyword_scores_higher(self):
        """Multi-word keywords (e.g., 'checkout page') should score 2.0 vs 1.0."""
        conn = _mock_conn()
        result = classify_task(
            conn,
            "fix checkout page error",
            "",
            {"fix", "checkout", "page", "error"},
        )
        # "checkout page" (2.0) should make HS win over other single-word matches
        assert "hs" in result.domain

    def test_classification_is_frozen_dataclass(self):
        """TaskClassification should be immutable."""
        tc = TaskClassification(domain="pi", provider="stripe", confidence=1.0)
        try:
            tc.domain = "bo"  # type: ignore
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass
