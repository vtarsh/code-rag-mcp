"""Tests for search/service.py — search_tool."""

from unittest.mock import patch


class TestSearchTool:
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_header_shows_candidates_format(self, mock_hybrid, mock_cache, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = (
            [
                {
                    "repo_name": "repo-a",
                    "file_path": "src/a.ts",
                    "file_type": "grpc_method",
                    "chunk_type": "function",
                    "snippet": "payment handler code",
                    "sources": ["keyword", "vector"],
                },
                {
                    "repo_name": "repo-b",
                    "file_path": "src/b.ts",
                    "file_type": "docs",
                    "chunk_type": "section",
                    "snippet": "docs about payment",
                    "sources": ["keyword"],
                },
            ],
            None,
            15,
        )
        result = search_tool("payment")
        assert "2 of 15 candidates" in result

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_keyword_only_label(self, mock_hybrid, mock_cache, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = (
            [
                {
                    "repo_name": "repo-a",
                    "file_path": "src/a.ts",
                    "file_type": "grpc_method",
                    "chunk_type": "function",
                    "snippet": "code",
                    "sources": ["keyword"],
                },
            ],
            "LanceDB not available",
            5,
        )
        result = search_tool("payment")
        assert "(keyword only)" in result
        assert "LanceDB not available" in result

    def test_empty_query_returns_error(self):
        from src.search.service import search_tool

        result = search_tool("")
        assert result.startswith("Error: 'query' parameter is required")

    def test_whitespace_query_returns_error(self):
        from src.search.service import search_tool

        result = search_tool("   ")
        assert result.startswith("Error: 'query' parameter is required")

    def test_missing_query_default_returns_error(self):
        """Regression: callers omitting `query` must get a clean error, not a traceback.

        Observed 74x KeyError('query') in logs/tool_calls.jsonl (16% failure rate
        on /tool/search). The daemon dispatcher now passes args.get('query', '')
        and search_tool validates the default.
        """
        from src.search.service import search_tool

        # Called with zero positional args — simulates daemon lambda when args={}
        result = search_tool()
        assert result.startswith("Error: 'query' parameter is required")
        assert "non-empty string" in result

    def test_none_query_returns_error(self):
        from src.search.service import search_tool

        result = search_tool(None)  # type: ignore[arg-type]
        assert result.startswith("Error: 'query' parameter is required")

    def test_non_string_query_returns_error(self):
        from src.search.service import search_tool

        result = search_tool(123)  # type: ignore[arg-type]
        assert result.startswith("Error: 'query' parameter is required")

    def test_daemon_dispatcher_missing_query_key(self):
        """Regression: the daemon TOOLS['search'] lambda must not raise KeyError
        when callers POST {} or omit 'query'. This was the root cause of 74
        failed search calls (see logs/tool_calls.jsonl 2026-04-05)."""
        import daemon as daemon_mod

        # The lambda should use .get('query', '') and route to search_tool,
        # which returns a clear error string — not raise KeyError.
        result = daemon_mod.TOOLS["search"]({})
        assert isinstance(result, str)
        assert result.startswith("Error: 'query' parameter is required")

    @patch(
        "src.container.check_db_health",
        return_value="Knowledge base not built yet. Run: python3 scripts/build_index.py",
    )
    def test_db_health_error(self, mock_health):
        from src.search.service import search_tool

        result = search_tool("payment")
        assert "Knowledge base not built" in result

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", return_value="cached result content")
    def test_cache_hit(self, mock_cache, mock_health):
        from src.search.service import search_tool

        result = search_tool("payment")
        assert result == "cached result content"

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    @patch("src.search.service.format_no_results", return_value="No results found")
    def test_no_results(self, mock_fmt, mock_hybrid, mock_cache, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = ([], None, 0)
        result = search_tool("xyzzy_nonexistent")
        assert result == "No results found"

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_repo_filter_in_header(self, mock_hybrid, mock_cache, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = (
            [
                {
                    "repo_name": "grpc-apm-trustly",
                    "file_path": "src/a.ts",
                    "file_type": "grpc_method",
                    "chunk_type": "function",
                    "snippet": "code",
                    "sources": ["keyword"],
                }
            ],
            None,
            1,
        )
        result = search_tool("payment", repo="trustly")
        assert "trustly" in result
