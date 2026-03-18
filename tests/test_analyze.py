"""Tests for tools/analyze.py — analyze_task_tool."""

import sqlite3
from unittest.mock import MagicMock, patch


def _mock_conn():
    """Create an in-memory SQLite DB with the schema analyze_task expects."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE repos (name TEXT, type TEXT)")
    conn.execute("CREATE TABLE graph_edges (source TEXT, target TEXT, edge_type TEXT)")
    # FTS5 table for chunks
    conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(repo_name, file_path, file_type, chunk_type, content)")
    return conn


class TestAnalyzeTaskTool:
    @patch(
        "src.container.check_db_health",
        return_value="Knowledge base not built yet. Run: python3 scripts/build_index.py",
    )
    def test_db_health_error(self, mock_health):
        from src.tools.analyze import analyze_task_tool

        result = analyze_task_tool("implement payment")
        assert "Knowledge base not built" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.get_db")
    def test_basic_output_structure(self, mock_get_db, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_get_db.return_value = conn
        result = analyze_task_tool("implement something new")
        assert "# Task Analysis" in result
        assert "Proto Contract" in result
        assert "Payment Gateway" in result
        assert "Completeness Report" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.get_db")
    def test_provider_autodetect(self, mock_get_db, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        conn.execute("INSERT INTO repos VALUES ('grpc-apm-trustly', 'service')")
        mock_get_db.return_value = conn
        result = analyze_task_tool("implement verification for trustly")
        assert "Provider: trustly" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.get_db")
    def test_explicit_provider(self, mock_get_db, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        conn.execute("INSERT INTO repos VALUES ('grpc-apm-paypal', 'service')")
        mock_get_db.return_value = conn
        result = analyze_task_tool("implement refund", provider="paypal")
        assert "Provider: paypal" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.get_db")
    def test_task_id_detection(self, mock_get_db, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_get_db.return_value = conn
        result = analyze_task_tool("PI-54 implement DirectDebitMandate for trustly")
        assert "pi-54" in result.lower()
        assert "Task ID detected" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.get_db")
    def test_no_task_id(self, mock_get_db, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_get_db.return_value = conn
        result = analyze_task_tool("generic task without id")
        assert "No task ID detected" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.get_db")
    def test_e2e_in_completeness(self, mock_get_db, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_get_db.return_value = conn
        result = analyze_task_tool("implement something")
        assert "e2e-tests" in result
        assert "E2E tests" in result


class TestCheckMethodExists:
    def test_method_found_in_chunks(self):
        from src.tools.analyze import _check_method_exists

        conn = _mock_conn()
        conn.execute(
            "INSERT INTO chunks VALUES ('repo-a', 'methods/refund', 'grpc_method', 'function', 'refund handler code')"
        )
        result = _check_method_exists("repo-a", "refund", conn)
        assert result["exists"] is True

    def test_method_not_found(self):
        from src.tools.analyze import _check_method_exists

        conn = _mock_conn()
        result = _check_method_exists("repo-a", "nonexistent", conn)
        assert result["exists"] is False


class TestGhApi:
    @patch("src.tools.analyze.subprocess.run")
    def test_success(self, mock_run):
        from src.tools.analyze import _gh_api

        mock_run.return_value = MagicMock(returncode=0, stdout='[{"name": "main"}]')
        result = _gh_api("repos/org/repo/branches")
        assert result == [{"name": "main"}]

    @patch("src.tools.analyze.subprocess.run", side_effect=Exception("timeout"))
    def test_failure(self, mock_run):
        from src.tools.analyze import _gh_api

        result = _gh_api("repos/org/repo/branches")
        assert result is None
