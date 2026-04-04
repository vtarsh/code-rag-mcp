"""Tests for search/hybrid.py — RRF fusion and reranking."""

from unittest.mock import MagicMock, patch

from src.search.hybrid import hybrid_search, rerank
from src.types import SearchResult


def _make_sr(rowid: int, repo: str = "repo-a", snippet: str = "test snippet") -> SearchResult:
    """Helper to build a SearchResult for FTS mock returns."""
    return SearchResult(
        rowid=rowid,
        repo_name=repo,
        file_path=f"src/{repo}/file.ts",
        file_type="grpc_method",
        chunk_type="function",
        snippet=snippet,
    )


def _make_vr(rowid: int, repo: str = "repo-b") -> dict:
    """Helper to build a vector search result dict."""
    return {
        "rowid": rowid,
        "repo_name": repo,
        "file_path": f"src/{repo}/vec.ts",
        "file_type": "docs",
        "chunk_type": "section",
        "content_preview": "vector preview",
    }


class TestHybridSearch:
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_empty_results(self, mock_fts, mock_vec, mock_rerank):
        results, err, total = hybrid_search("nonexistent query")
        assert results == []
        assert err is None
        assert total == 0

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search")
    def test_keyword_only(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(1), _make_sr(2)]
        results, err, total = hybrid_search("payment")
        assert len(results) == 2
        assert err is None
        assert total >= len(results)

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_vector_only(self, mock_fts, mock_vec, mock_rerank):
        mock_vec.return_value = ([_make_vr(10), _make_vr(11)], None)
        results, _err, total = hybrid_search("semantic query")
        assert len(results) == 2
        assert total == 2

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_vector_error_propagated(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(1)]
        mock_vec.return_value = ([], "LanceDB not found")
        _results, err, total = hybrid_search("payment")
        assert err == "LanceDB not found"
        assert total >= 1

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_rrf_fusion_merges_sources(self, mock_fts, mock_vec, mock_rerank):
        # Same rowid from both sources — should merge
        mock_fts.return_value = [_make_sr(42)]
        mock_vec.return_value = ([_make_vr(42)], None)
        results, _err, total = hybrid_search("overlapping")
        assert total == 1  # deduplicated by rowid
        assert "keyword" in results[0]["sources"]
        assert "vector" in results[0]["sources"]

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_total_candidates_gte_results(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(i) for i in range(15)]
        mock_vec.return_value = ([_make_vr(i + 100) for i in range(10)], None)
        results, _err, total = hybrid_search("big query", limit=5)
        assert total == 25
        assert total >= len(results)

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_keyword_weight_higher(self, mock_fts, mock_vec, mock_rerank):
        # Keyword-only result should score higher than vector-only due to 2x weight
        mock_fts.return_value = [_make_sr(1)]
        mock_vec.return_value = ([_make_vr(2)], None)
        results, _, _ = hybrid_search("test", limit=10)
        keyword_result = next(r for r in results if "keyword" in r["sources"])
        vector_result = next(r for r in results if "vector" in r["sources"] and "keyword" not in r["sources"])
        assert keyword_result["score"] > vector_result["score"]

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_returns_3_tuple(self, mock_fts, mock_vec, mock_rerank):
        result = hybrid_search("anything")
        assert isinstance(result, tuple)
        assert len(result) == 3


class TestRerank:
    def test_empty_results(self):
        assert rerank("query", []) == []

    def test_single_result_passthrough(self):
        single = [{"score": 1.0, "snippet": "test", "repo_name": "r", "file_path": "f"}]
        assert rerank("query", single) == single

    @patch("src.search.hybrid.get_reranker")
    def test_reranker_unavailable_fallback(self, mock_get_reranker):
        mock_get_reranker.return_value = (None, "model not loaded")
        items = [
            {"score": 0.5, "snippet": "a", "repo_name": "r1", "file_path": "f1"},
            {"score": 0.3, "snippet": "b", "repo_name": "r2", "file_path": "f2"},
        ]
        result = rerank("query", items)
        assert result == items  # unchanged

    @patch("src.search.hybrid.get_reranker")
    def test_reranker_scores_combined(self, mock_get_reranker):
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.9, 0.1]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.3, "snippet": "low rrf high rerank", "repo_name": "r1", "file_path": "f1"},
            {"score": 0.5, "snippet": "high rrf low rerank", "repo_name": "r2", "file_path": "f2"},
        ]
        result = rerank("query", items, limit=2)
        # First item has higher rerank score (0.9), should be first after combining
        assert result[0]["repo_name"] == "r1"
        assert "combined_score" in result[0]
        assert "rerank_score" in result[0]
