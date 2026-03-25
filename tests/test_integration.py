"""Integration tests — end-to-end tests against the real knowledge.db.

These tests verify the full pipeline (FTS5 + vector search + reranking,
graph traversal, analyze_task orchestration) with real indexed data.

Skip gracefully if the database does not exist.
Run separately: python -m pytest tests/test_integration.py -v -m integration
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

# DB path mirrors src/config.py logic
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _PROJECT_ROOT / "db" / "knowledge.db"

_db_exists = _DB_PATH.exists()
_skip_reason = "knowledge.db not found — run build_index.py first"

pytestmark = pytest.mark.integration


def _db_has_table(table: str) -> bool:
    """Check if a table exists in the database."""
    if not _db_exists:
        return False
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row is not None
    finally:
        conn.close()


def _pick_repo_with_edges() -> str | None:
    """Return a repo name that has graph edges, or None."""
    if not _db_exists:
        return None
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        row = conn.execute("SELECT source FROM graph_edges GROUP BY source ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _db_exists, reason=_skip_reason)
class TestSearchIntegration:
    """End-to-end search through the full hybrid pipeline."""

    def test_search_returns_results(self):
        """search('payment gateway') should return non-empty results."""
        from src.search.service import search_tool

        result = search_tool("payment gateway")
        assert isinstance(result, str)
        # Should contain at least one repo reference or result block
        assert "payment" in result.lower() or "gateway" in result.lower()
        # Should NOT be an error or empty-result message
        assert "Error:" not in result

    def test_search_with_repo_filter(self):
        """search with repo='grpc-payment-gateway' should only return results from that repo."""
        from src.search.service import search_tool

        result = search_tool("payment", repo="grpc-payment-gateway")
        assert isinstance(result, str)
        # If results are found, they should reference the filtered repo
        if "No results" not in result and "not found" not in result.lower():
            assert "grpc-payment-gateway" in result

    def test_search_empty_query_returns_error(self):
        """Empty query should return an error string, not crash."""
        from src.search.service import search_tool

        result = search_tool("")
        assert "Error" in result or "empty" in result.lower()

    def test_search_with_exclude_file_types(self):
        """Excluding file types should not crash and should return results."""
        from src.search.service import search_tool

        result = search_tool("webhook handler", exclude_file_types="gotchas,task")
        assert isinstance(result, str)
        assert "Error:" not in result


# ---------------------------------------------------------------------------
# analyze_task tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _db_exists, reason=_skip_reason)
class TestAnalyzeTaskIntegration:
    """End-to-end analyze_task with real DB — mock only GitHub API calls."""

    @patch("src.tools.analyze.github_helpers.find_task_prs", return_value={})
    @patch("src.tools.analyze.github_helpers.find_task_branches", return_value={})
    def test_analyze_task_pi(self, mock_branches, mock_prs):
        """PI task description should find provider-related repos."""
        from src.tools.analyze import analyze_task_tool

        result = analyze_task_tool(
            "PI-100 implement DirectDebitMandate for Trustly provider",
            provider="trustly",
        )
        assert isinstance(result, str)
        assert "# Task Analysis" in result
        assert "trustly" in result.lower()
        # Should classify as PI domain
        assert "**Domain**: pi" in result or "**Domain**: PI" in result

    @patch("src.tools.analyze.github_helpers.find_task_prs", return_value={})
    @patch("src.tools.analyze.github_helpers.find_task_branches", return_value={})
    def test_analyze_task_core(self, mock_branches, mock_prs):
        """CORE task description should find core infrastructure repos."""
        from src.tools.analyze import analyze_task_tool

        result = analyze_task_tool("CORE-200 add new field to payment transaction model")
        assert isinstance(result, str)
        assert "# Task Analysis" in result
        # Should find some repos
        assert "Repos found" in result

    @patch("src.tools.analyze.github_helpers.find_task_prs", return_value={})
    @patch("src.tools.analyze.github_helpers.find_task_branches", return_value={})
    def test_analyze_task_returns_repo_counts(self, mock_branches, mock_prs):
        """analyze_task should report repo tier counts in the summary."""
        from src.tools.analyze import analyze_task_tool

        result = analyze_task_tool("BO-50 update backoffice transaction list filters")
        assert isinstance(result, str)
        assert "Repos found" in result
        # Should have the format: N core + N related + N peripheral
        assert "core" in result.lower()


# ---------------------------------------------------------------------------
# Graph tool tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _db_exists, reason=_skip_reason)
@pytest.mark.skipif(
    not _db_has_table("graph_edges"),
    reason="graph_edges table not found",
)
class TestGraphIntegration:
    """End-to-end graph traversal with real data."""

    def test_find_dependencies(self):
        """find_dependencies for a known repo should return edges."""
        from src.graph.service import find_dependencies_tool

        repo = _pick_repo_with_edges()
        if repo is None:
            pytest.skip("no repos with edges found")

        result = find_dependencies_tool(repo)
        assert isinstance(result, str)
        assert "Dependencies for" in result
        # Should have at least one edge section
        assert "depends on" in result.lower() or "depending on" in result.lower()

    def test_find_dependencies_unknown_repo(self):
        """Unknown repo should return an error, not crash."""
        from src.graph.service import find_dependencies_tool

        result = find_dependencies_tool("nonexistent-repo-xyz-999")
        assert isinstance(result, str)
        # Should indicate repo not found
        assert "not found" in result.lower() or "no repo" in result.lower() or "error" in result.lower()

    def test_trace_impact(self):
        """trace_impact for a known repo should return affected repos."""
        from src.graph.service import trace_impact_tool

        # Use a repo known to have many dependents
        result = trace_impact_tool("libs-types", depth=1)
        assert isinstance(result, str)
        assert "Impact Analysis" in result
        assert "affected repos" in result.lower()

    def test_trace_impact_depth_clamped(self):
        """Excessive depth should be clamped, not error."""
        from src.graph.service import trace_impact_tool

        repo = _pick_repo_with_edges()
        if repo is None:
            pytest.skip("no repos with edges found")

        result = trace_impact_tool(repo, depth=99)
        assert isinstance(result, str)
        assert "Impact Analysis" in result


# ---------------------------------------------------------------------------
# Health check test
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _db_exists, reason=_skip_reason)
class TestHealthCheckIntegration:
    """End-to-end health_check with real database and models."""

    def test_health_check_returns_valid_status(self):
        """health_check should return a diagnostic report."""
        from src.tools.service import health_check_tool

        result = health_check_tool()
        assert isinstance(result, str)
        assert "Health Check" in result
        # Should report on key components
        assert "repos" in result.lower() or "chunks" in result.lower()

    def test_health_check_reports_counts(self):
        """health_check should report numeric counts for repos and chunks."""
        from src.tools.service import health_check_tool

        result = health_check_tool()
        # Should contain at least one number (repo count, chunk count, etc.)
        import re

        numbers = re.findall(r"\d+", result)
        assert len(numbers) > 0, "health_check should report numeric stats"
