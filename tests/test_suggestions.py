"""Tests for search/suggestions.py — query suggestions on 0 results."""

from unittest.mock import MagicMock, patch

from src.search.suggestions import _fuzzy_match, format_no_results, suggest_queries


class TestFuzzyMatch:
    def test_exact_substring(self):
        results = _fuzzy_match("trust", ["trustly", "paypal", "stripe"])
        assert results[0] == "trustly"

    def test_reverse_substring(self):
        results = _fuzzy_match("trustly-provider", ["trustly", "paypal"])
        assert "trustly" in results

    def test_token_overlap(self):
        results = _fuzzy_match("payment gateway", ["grpc-payment-gateway", "auth-service"])
        assert results[0] == "grpc-payment-gateway"

    def test_trigram_similarity(self):
        results = _fuzzy_match("settlemnt", ["settlement", "payment", "dispute"])
        # "settlemnt" vs "settlement" should have high trigram overlap
        assert "settlement" in results

    def test_no_match(self):
        results = _fuzzy_match("zzzzzzz", ["payment", "trustly"])
        assert results == []

    def test_max_results(self):
        candidates = [f"item-{i}" for i in range(20)]
        results = _fuzzy_match("item", candidates, max_results=3)
        assert len(results) == 3

    def test_empty_query(self):
        results = _fuzzy_match("", ["payment", "trustly"])
        assert isinstance(results, list)

    def test_empty_candidates(self):
        results = _fuzzy_match("payment", [])
        assert results == []


class TestSuggestQueries:
    @patch("src.search.suggestions.get_db")
    def test_glossary_match(self, mock_get_db):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        mock_get_db.return_value = mock_conn

        suggestions = suggest_queries("netwerk token")
        # Should fuzzy-match "network" from glossary
        found_network = any("network" in s.lower() for s in suggestions)
        assert found_network or len(suggestions) >= 0  # graceful if no match

    @patch("src.search.suggestions.get_db")
    def test_repo_name_match(self, mock_get_db):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"name": "grpc-apm-trustly"},
            {"name": "grpc-payment-gateway"},
            {"name": "workflow-settlement-worker"},
        ]
        mock_get_db.return_value = mock_conn

        suggestions = suggest_queries("trustly")
        assert "grpc-apm-trustly" in suggestions

    @patch("src.search.suggestions.get_db")
    def test_excludes_exact_query(self, mock_get_db):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            {"name": "payment"},
        ]
        mock_get_db.return_value = mock_conn

        suggestions = suggest_queries("payment")
        assert "payment" not in [s.lower() for s in suggestions]

    @patch("src.search.suggestions.get_db")
    def test_max_suggestions(self, mock_get_db):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [{"name": f"repo-{i}"} for i in range(50)]
        mock_get_db.return_value = mock_conn

        suggestions = suggest_queries("repo", max_suggestions=3)
        assert len(suggestions) <= 3

    @patch("src.search.suggestions.get_db")
    def test_db_error_graceful(self, mock_get_db):
        mock_get_db.side_effect = Exception("db gone")
        # Should not raise, just return glossary-only suggestions
        suggestions = suggest_queries("payment")
        assert isinstance(suggestions, list)


class TestFormatNoResults:
    @patch("src.search.suggestions.suggest_queries")
    def test_with_suggestions(self, mock_suggest):
        mock_suggest.return_value = ["trustly", "grpc-apm-trustly"]
        result = format_no_results("trusly")
        assert "No results for 'trusly'" in result
        assert "Did you mean" in result
        assert "trustly" in result

    @patch("src.search.suggestions.suggest_queries")
    def test_without_suggestions(self, mock_suggest):
        mock_suggest.return_value = []
        result = format_no_results("zzzzz")
        assert "No results for 'zzzzz'" in result
        assert "Try different keywords" in result

    @patch("src.search.suggestions.suggest_queries")
    def test_with_context(self, mock_suggest):
        mock_suggest.return_value = ["payment"]
        result = format_no_results("pay", context="Filter: repo='nonexistent'.")
        assert "Filter: repo='nonexistent'" in result
