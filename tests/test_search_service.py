"""Tests for search/service.py — search_tool."""

from unittest.mock import patch


class TestSearchTool:
    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_get", return_value=None)
    @patch("src.search.service.cache_set")
    @patch("src.search.service.hybrid_search")
    def test_header_shows_candidates_format(self, mock_hybrid, mock_cset, mock_cget, mock_health):
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
    @patch("src.search.service.cache_get", return_value=None)
    @patch("src.search.service.cache_set")
    @patch("src.search.service.hybrid_search")
    def test_keyword_only_label(self, mock_hybrid, mock_cset, mock_cget, mock_health):
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
        assert result == "Error: query cannot be empty"

    def test_whitespace_query_returns_error(self):
        from src.search.service import search_tool

        result = search_tool("   ")
        assert result == "Error: query cannot be empty"

    @patch(
        "src.container.check_db_health",
        return_value="Knowledge base not built yet. Run: python3 scripts/build_index.py",
    )
    def test_db_health_error(self, mock_health):
        from src.search.service import search_tool

        result = search_tool("payment")
        assert "Knowledge base not built" in result

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_get")
    def test_cache_hit(self, mock_cget, mock_health):
        from src.search.service import search_tool

        mock_cget.return_value = "cached result content"
        result = search_tool("payment")
        assert result == "cached result content"

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_get", return_value=None)
    @patch("src.search.service.cache_set")
    @patch("src.search.service.hybrid_search")
    @patch("src.search.service.format_no_results", return_value="No results found")
    def test_no_results(self, mock_fmt, mock_hybrid, mock_cset, mock_cget, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = ([], None, 0)
        result = search_tool("xyzzy_nonexistent")
        assert result == "No results found"

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_get", return_value=None)
    @patch("src.search.service.cache_set")
    @patch("src.search.service.hybrid_search")
    def test_repo_filter_in_header(self, mock_hybrid, mock_cset, mock_cget, mock_health):
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
