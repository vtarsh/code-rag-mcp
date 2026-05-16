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

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    @patch("src.search.service.expand_query_dictionary", return_value="auth_code provider authCode")
    def test_dictionary_expand_wired(self, mock_expand_dict, mock_hybrid, mock_cache, mock_health):
        import os

        from src.search.service import search_tool

        os.environ["CODE_RAG_USE_DICTIONARY_EXPAND"] = "1"
        try:
            mock_hybrid.return_value = ([], None, 0)
            search_tool("auth_code provider")
            mock_expand_dict.assert_called_once()
        finally:
            del os.environ["CODE_RAG_USE_DICTIONARY_EXPAND"]

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    @patch("src.search.service.expand_query_dictionary")
    def test_dictionary_expand_disabled_by_default(self, mock_expand_dict, mock_hybrid, mock_cache, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = ([], None, 0)
        search_tool("auth_code provider")
        mock_expand_dict.assert_not_called()

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_entity_boost_used_for_long_query_with_entities(self, mock_hybrid, mock_cache, mock_health):
        """Long query (>=6 words) with entities triggers entity_boost=1.3."""
        from src.search.service import search_tool

        mock_hybrid.return_value = (
            [
                {
                    "repo_name": "r1",
                    "file_path": "a.ts",
                    "file_type": "code",
                    "chunk_type": "function",
                    "snippet": "s1",
                    "sources": ["keyword"],
                },
                {
                    "repo_name": "r2",
                    "file_path": "b.ts",
                    "file_type": "code",
                    "chunk_type": "function",
                    "snippet": "s2",
                    "sources": ["keyword"],
                },
                {
                    "repo_name": "r3",
                    "file_path": "c.ts",
                    "file_type": "code",
                    "chunk_type": "function",
                    "snippet": "s3",
                    "sources": ["keyword"],
                },
                {
                    "repo_name": "r4",
                    "file_path": "d.ts",
                    "file_type": "code",
                    "chunk_type": "function",
                    "snippet": "s4",
                    "sources": ["keyword"],
                },
                {
                    "repo_name": "r5",
                    "file_path": "e.ts",
                    "file_type": "code",
                    "chunk_type": "function",
                    "snippet": "s5",
                    "sources": ["keyword"],
                },
            ],
            None,
            10,
        )
        search_tool("ProviderError throw payout input validation for nuvei merchant")
        assert mock_hybrid.call_count == 1
        _, kwargs = mock_hybrid.call_args
        assert kwargs.get("entity_boost") == 1.3
        # The processed query should only contain entities.
        assert "nuvei" in mock_hybrid.call_args[0][0]
        assert "ProviderError" in mock_hybrid.call_args[0][0]

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_fallback_when_entity_boost_returns_few_results(self, mock_hybrid, mock_cache, mock_health):
        """If entity-boosted search returns <5 results, fall back to original query."""
        from src.search.service import search_tool

        # First call returns 2 results (below 5 threshold), second call returns 10.
        mock_hybrid.side_effect = [
            (
                [
                    {
                        "repo_name": "r1",
                        "file_path": "a.ts",
                        "file_type": "code",
                        "chunk_type": "function",
                        "snippet": "s1",
                        "sources": ["keyword"],
                    },
                    {
                        "repo_name": "r2",
                        "file_path": "b.ts",
                        "file_type": "code",
                        "chunk_type": "function",
                        "snippet": "s2",
                        "sources": ["keyword"],
                    },
                ],
                None,
                2,
            ),
            (
                [
                    {
                        "repo_name": "r1",
                        "file_path": "a.ts",
                        "file_type": "code",
                        "chunk_type": "function",
                        "snippet": "s1",
                        "sources": ["keyword"],
                    },
                    {
                        "repo_name": "r2",
                        "file_path": "b.ts",
                        "file_type": "code",
                        "chunk_type": "function",
                        "snippet": "s2",
                        "sources": ["keyword"],
                    },
                    {
                        "repo_name": "r3",
                        "file_path": "c.ts",
                        "file_type": "code",
                        "chunk_type": "function",
                        "snippet": "s3",
                        "sources": ["keyword"],
                    },
                    {
                        "repo_name": "r4",
                        "file_path": "d.ts",
                        "file_type": "code",
                        "chunk_type": "function",
                        "snippet": "s4",
                        "sources": ["keyword"],
                    },
                    {
                        "repo_name": "r5",
                        "file_path": "e.ts",
                        "file_type": "code",
                        "chunk_type": "function",
                        "snippet": "s5",
                        "sources": ["keyword"],
                    },
                ],
                None,
                10,
            ),
        ]
        result = search_tool("ProviderError throw payout input validation for nuvei merchant")
        assert mock_hybrid.call_count == 2
        # Second call should use the original expanded query.
        assert "ProviderError throw payout input validation for nuvei merchant" in mock_hybrid.call_args[0][0]
        assert "Found 5 of 10 candidates" in result

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_no_entity_boost_for_short_query(self, mock_hybrid, mock_cache, mock_health):
        """Short queries (<6 words) should not trigger entity boost even if entities exist."""
        from src.search.service import search_tool

        mock_hybrid.return_value = (
            [
                {
                    "repo_name": "r1",
                    "file_path": "a.ts",
                    "file_type": "code",
                    "chunk_type": "function",
                    "snippet": "s1",
                    "sources": ["keyword"],
                }
            ],
            None,
            1,
        )
        search_tool("nuvei payout")
        assert mock_hybrid.call_count == 1
        _, kwargs = mock_hybrid.call_args
        assert kwargs.get("entity_boost") == 1.0

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_no_entity_boost_when_no_entities(self, mock_hybrid, mock_cache, mock_health):
        """Long query without extractable entities should use original query."""
        from src.search.service import search_tool

        mock_hybrid.return_value = (
            [
                {
                    "repo_name": "r1",
                    "file_path": "a.ts",
                    "file_type": "code",
                    "chunk_type": "function",
                    "snippet": "s1",
                    "sources": ["keyword"],
                }
            ],
            None,
            1,
        )
        search_tool("Fix All Tasks Tab Filter to Show Group Tasks")
        assert mock_hybrid.call_count == 1
        _, kwargs = mock_hybrid.call_args
        assert kwargs.get("entity_boost") == 1.0
        assert mock_hybrid.call_args[0][0] == "Fix All Tasks Tab Filter to Show Group Tasks"


class TestPreprocessQuery:
    def test_extracts_provider_names(self):
        from src.search.service import preprocess_query

        processed, entities = preprocess_query("ProviderError throw payout input validation for nuvei merchant")
        assert "nuvei" in entities
        assert "ProviderError" in entities
        assert processed == " ".join(entities)

    def test_extracts_file_extensions(self):
        from src.search.service import preprocess_query

        processed, entities = preprocess_query("Fix the validation.ts file and also check handler.go")
        assert ".ts" in entities
        assert ".go" in entities

    def test_extracts_error_classes(self):
        from src.search.service import preprocess_query

        processed, entities = preprocess_query("NotFoundError thrown when ValidationError occurs in service")
        assert "NotFoundError" in entities
        assert "ValidationError" in entities

    def test_extracts_all_caps_identifiers(self):
        from src.search.service import preprocess_query

        processed, entities = preprocess_query("The MAX_RETRY_COUNT and API_BASE_URL are not set")
        assert "MAX_RETRY_COUNT" in entities
        assert "API_BASE_URL" in entities

    def test_extracts_repo_names(self):
        from src.search.service import preprocess_query

        processed, entities = preprocess_query("Update the grpc-payment-gateway logic for nuvei payout")
        # grpc-payment-gateway is a known repo from conventions.yaml
        assert "grpc-payment-gateway" in entities
        assert "nuvei" in entities

    def test_returns_original_when_no_entities(self):
        from src.search.service import preprocess_query

        processed, entities = preprocess_query("Fix All Tasks Tab Filter to Show Group Tasks")
        assert entities == []
        assert processed == "Fix All Tasks Tab Filter to Show Group Tasks"

    def test_returns_empty_for_empty_query(self):
        from src.search.service import preprocess_query

        processed, entities = preprocess_query("")
        assert entities == []
        assert processed == ""
