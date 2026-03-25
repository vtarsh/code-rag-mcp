"""Tests for tools/analyze.py — analyze_task_tool."""

import sqlite3
from contextlib import contextmanager
from unittest.mock import MagicMock, patch


def _mock_conn():
    """Create an in-memory SQLite DB with the schema analyze_task expects."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE repos (name TEXT, type TEXT)")
    conn.execute("CREATE TABLE graph_edges (source TEXT, target TEXT, edge_type TEXT, detail TEXT)")
    # FTS5 table for chunks
    conn.execute("CREATE VIRTUAL TABLE chunks USING fts5(repo_name, file_path, file_type, chunk_type, content)")
    return conn


def _mock_db_connection(conn):
    """Create a mock db_connection context manager that yields conn."""

    @contextmanager
    def _cm():
        yield conn

    return _cm


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
    @patch("src.tools.analyze.db_connection")
    def test_basic_output_structure(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_db_conn.side_effect = _mock_db_connection(conn)
        result = analyze_task_tool("implement something new")
        assert "# Task Analysis" in result
        assert "Proto Contract" in result
        assert "Payment Gateway" in result
        assert "Completeness Report" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.db_connection")
    def test_provider_autodetect(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        conn.execute("INSERT INTO repos VALUES ('grpc-apm-trustly', 'service')")
        mock_db_conn.side_effect = _mock_db_connection(conn)
        result = analyze_task_tool("implement verification for trustly")
        assert "Provider: trustly" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.db_connection")
    def test_explicit_provider(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        conn.execute("INSERT INTO repos VALUES ('grpc-apm-paypal', 'service')")
        mock_db_conn.side_effect = _mock_db_connection(conn)
        result = analyze_task_tool("implement refund", provider="paypal")
        assert "Provider: paypal" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.db_connection")
    def test_task_id_detection(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_db_conn.side_effect = _mock_db_connection(conn)
        result = analyze_task_tool("PI-54 implement DirectDebitMandate for trustly")
        assert "pi-54" in result.lower()
        assert "Task ID detected" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.db_connection")
    def test_no_task_id(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_db_conn.side_effect = _mock_db_connection(conn)
        result = analyze_task_tool("generic task without id")
        assert "No task ID detected" in result

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.db_connection")
    def test_e2e_in_completeness(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        mock_db_conn.side_effect = _mock_db_connection(conn)
        result = analyze_task_tool("implement something")
        assert "e2e-tests" in result
        assert "E2E tests" in result


class TestClassifierMultiDomain:
    """Test classifier multi-domain detection for BO/HS prefix tasks."""

    def test_bo_prefix_gets_secondary_domain_seeds(self):
        from src.tools.analyze.classifier import classify_task

        conn = _mock_conn()
        # BO-1598 "High Risk Override Reason Logic" should match bo + core-risk
        words = {"high", "risk", "override", "reason", "logic"}
        result = classify_task(conn, "Improve High Risk Override Reason Logic BO-1598", "", words)
        assert result.domain.startswith("bo")
        assert result.confidence > 0
        # Should have core-risk seed repos merged in
        seed_names = set(result.seed_repos)
        assert "graphql" in seed_names or "backoffice-web" in seed_names  # BO seeds
        assert any("risk" in s for s in seed_names)  # core-risk seeds

    def test_bo_prefix_without_keywords_stays_bo(self):
        from src.tools.analyze.classifier import classify_task

        conn = _mock_conn()
        words = {"add", "tabs", "section"}
        result = classify_task(conn, "Add Two Tabs to Collaborations Section BO-1603", "", words)
        assert result.domain == "bo"
        assert "graphql" in result.seed_repos or "backoffice-web" in result.seed_repos

    def test_core_prefix_unchanged(self):
        from src.tools.analyze.classifier import classify_task

        conn = _mock_conn()
        words = {"copy", "partial", "approval", "controls", "payment", "session", "routes"}
        result = classify_task(conn, "[API] Copy partial approval controls CORE-2582", "", words)
        # CORE should still go through keyword matching, not prefix shortcut
        assert not result.domain.startswith("bo")


class TestBidirectionalCoOccurrence:
    """Test bidirectional co-occurrence catches tight satellites."""

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.db_connection")
    def test_reverse_probability_adds_satellite(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        import json

        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        conn.execute(
            "CREATE TABLE task_history (id INTEGER PRIMARY KEY, ticket_id TEXT, summary TEXT, repos_changed TEXT, description TEXT, ticket_type TEXT, developer TEXT, epic_id TEXT, parent_id TEXT, subtasks TEXT, linked_issues TEXT, labels TEXT, components TEXT, sprint TEXT, story_points TEXT, jira_status TEXT, status_changelog TEXT, jira_comments TEXT, attachments TEXT, files_changed TEXT, pr_urls TEXT, pr_review_comments TEXT, bugs_linked TEXT, custom_fields TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE task_history_fts USING fts5(summary, description, content=task_history, content_rowid=id)"
        )
        conn.execute("INSERT INTO repos VALUES ('express-api-v1', 'service')")
        conn.execute("INSERT INTO repos VALUES ('grpc-auth-apikeys2', 'service')")
        # Create 5 CORE tasks where apikeys2 ALWAYS appears with express-api-v1
        for i in range(5):
            repos = json.dumps(["express-api-v1", "grpc-auth-apikeys2", "grpc-core-schemas"])
            conn.execute(
                "INSERT INTO task_history (ticket_id, summary, repos_changed) VALUES (?, ?, ?)",
                (f"CORE-{2600 + i}", f"API task {i}", repos),
            )
        # Create 15 CORE tasks where express-api-v1 appears WITHOUT apikeys2
        for i in range(15):
            repos = json.dumps(["express-api-v1", "libs-types"])
            conn.execute(
                "INSERT INTO task_history (ticket_id, summary, repos_changed) VALUES (?, ?, ?)",
                (f"CORE-{2700 + i}", f"Other task {i}", repos),
            )
        mock_db_conn.side_effect = _mock_db_connection(conn)

        result = analyze_task_tool("CORE-2650 Add new API endpoint for settlements")
        # P(apikeys2 | express-api-v1) = 5/20 = 25% (below 40% threshold)
        # P(express-api-v1 | apikeys2) = 5/5 = 100% (above 80% reverse threshold)
        # Bidirectional should catch this
        assert "grpc-auth-apikeys2" in result


class TestSimilarTaskBoost:
    """Test similar-task boost with self-match exclusion."""

    @patch("src.tools.analyze._find_task_prs", return_value={})
    @patch("src.tools.analyze._find_task_branches", return_value={})
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.tools.analyze.db_connection")
    def test_self_match_excluded(self, mock_db_conn, mock_health, mock_branches, mock_prs):
        import json

        from src.tools.analyze import analyze_task_tool

        conn = _mock_conn()
        conn.execute(
            "CREATE TABLE task_patterns (pattern_type TEXT, missed_repo TEXT, trigger_repos TEXT, occurrences INT, confidence REAL)"
        )
        conn.execute(
            "CREATE TABLE task_history (id INTEGER PRIMARY KEY, ticket_id TEXT, summary TEXT, repos_changed TEXT, description TEXT, ticket_type TEXT, developer TEXT, epic_id TEXT, parent_id TEXT, subtasks TEXT, linked_issues TEXT, labels TEXT, components TEXT, sprint TEXT, story_points TEXT, jira_status TEXT, status_changelog TEXT, jira_comments TEXT, attachments TEXT, files_changed TEXT, pr_urls TEXT, pr_review_comments TEXT, bugs_linked TEXT, custom_fields TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE task_history_fts USING fts5(summary, description, content=task_history, content_rowid=id)"
        )
        # Insert the task itself
        conn.execute(
            "INSERT INTO task_history (ticket_id, summary, repos_changed) VALUES (?, ?, ?)",
            ("CORE-100", "Fix audit logging", json.dumps(["repo-a", "repo-b", "repo-c"])),
        )
        conn.execute("INSERT INTO task_history_fts (rowid, summary, description) VALUES (1, 'Fix audit logging', '')")
        mock_db_conn.side_effect = _mock_db_connection(conn)

        result = analyze_task_tool("Fix audit logging CORE-100")
        # Should NOT inject repos from self-match — repo-a/b/c should NOT appear as similar_task findings
        assert "Similar past task" not in result

    def test_similar_task_boost_logic(self):
        """Test the similar-task boost logic directly: >=3 repo overlap triggers injection."""
        from src.tools.analyze.base import AnalysisContext, Finding

        conn = _mock_conn()
        ctx = AnalysisContext(conn=conn, description="test", words=set(), provider="")
        # Pre-populate findings with 3 repos
        ctx.findings = [
            Finding("domain", "repo-a", "high"),
            Finding("domain", "repo-b", "high"),
            Finding("domain", "repo-c", "high"),
        ]

        # Simulate similar task with 4 repos (3 overlap + 1 new)
        similar_repos = ["repo-a", "repo-b", "repo-c", "repo-new"]
        existing = {f.repo for f in ctx.findings}
        overlap = existing & set(similar_repos)
        assert len(overlap) >= 3  # overlap threshold met

        # Inject new repos
        for repo in similar_repos:
            if repo not in existing:
                ctx.findings.append(Finding("similar_task", repo, "medium"))
                existing.add(repo)

        assert Finding("similar_task", "repo-new", "medium") in ctx.findings
        assert len(ctx.findings) == 4  # 3 original + 1 injected

    def test_similar_task_no_boost_below_threshold(self):
        """No injection when overlap < 3 repos."""
        from src.tools.analyze.base import AnalysisContext, Finding

        conn = _mock_conn()
        ctx = AnalysisContext(conn=conn, description="test", words=set(), provider="")
        ctx.findings = [Finding("domain", "repo-a", "high"), Finding("domain", "repo-b", "high")]

        similar_repos = ["repo-a", "repo-b", "repo-c", "repo-new"]
        existing = {f.repo for f in ctx.findings}
        overlap = existing & set(similar_repos)
        assert len(overlap) == 2  # below threshold — no injection


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
        from src.tools.analyze import _clear_gh_cache, _gh_api

        _clear_gh_cache()  # Ensure no cached result from test_success
        result = _gh_api("repos/org/repo/branches")
        assert result is None
