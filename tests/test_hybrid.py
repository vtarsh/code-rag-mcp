"""Tests for search/hybrid.py — RRF fusion and reranking."""

from unittest.mock import MagicMock, patch

import pytest

from src.search.hybrid import hybrid_search, rerank
from src.types import SearchResult


@pytest.fixture(autouse=True)
def _mock_wiring():
    """P0c: Suppress code_facts/env_vars wiring in hybrid tests by default.

    Individual tests can override these patches to assert the wiring behaviour.
    Without this fixture, every test would hit the live knowledge.db and pull
    extra candidates into the pool, breaking existing assertions.
    """
    with patch("src.search.hybrid.code_facts_search", return_value=[]), patch(
        "src.search.hybrid.env_var_search", return_value=[]
    ):
        yield


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


class TestCodeFactsWiring:
    """P0c: code_facts_fts feeds the RRF pool via boost-existing + inject-missing."""

    @patch("src.search.hybrid.fetch_chunks_for_files", return_value=[])
    @patch("src.search.hybrid.env_var_search", return_value=[])
    @patch("src.search.hybrid.code_facts_search")
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search")
    def test_existing_file_gets_code_fact_boost(
        self, mock_fts, mock_vec, mock_rerank, mock_cf, mock_ev, mock_fetch
    ):
        sr_hit = _make_sr(7, repo="repo-a")
        sr_other = _make_sr(8, repo="repo-b")
        mock_fts.return_value = [sr_hit, sr_other]
        mock_cf.return_value = [
            {
                "repo_name": sr_hit.repo_name,
                "file_path": sr_hit.file_path,
                "fact_type": "joi_schema",
                "condition": "x",
                "message": "",
                "line_number": 1,
            }
        ]
        results, _err, total = hybrid_search("query with joi schema", limit=10)
        # Same two files, no injection since fetch_chunks_for_files is empty
        assert total == 2
        boosted = next(r for r in results if r["repo_name"] == "repo-a")
        other = next(r for r in results if r["repo_name"] == "repo-b")
        assert "code_facts" in boosted["sources"]
        assert "code_facts" not in other["sources"]
        assert boosted["score"] > other["score"]

    @patch("src.search.hybrid.env_var_search", return_value=[])
    @patch("src.search.hybrid.fetch_chunks_for_files")
    @patch("src.search.hybrid.code_facts_search")
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_missing_file_injected_from_code_facts(
        self, mock_fts, mock_vec, mock_rerank, mock_cf, mock_fetch, mock_ev
    ):
        mock_cf.return_value = [
            {
                "repo_name": "new-repo",
                "file_path": "libs/fresh.js",
                "fact_type": "validation_guard",
                "condition": "x",
                "message": "",
                "line_number": 10,
            }
        ]
        mock_fetch.return_value = [
            {
                "rowid": 999,
                "repo_name": "new-repo",
                "file_path": "libs/fresh.js",
                "file_type": "library",
                "chunk_type": "code_file",
                "snippet": "fresh content",
            }
        ]

        results, _err, total = hybrid_search("signature validation", limit=10)
        assert total == 1  # only the injected candidate
        assert results[0]["repo_name"] == "new-repo"
        assert results[0]["sources"] == ["code_facts"]
        # injected score must be positive
        assert results[0]["score"] > 0

    @patch("src.search.hybrid.env_var_search", return_value=[])
    @patch("src.search.hybrid.code_facts_search", return_value=[])
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_no_code_facts_no_change(self, *_mocks):
        # Empty code_facts hits → pool stays empty
        results, _err, total = hybrid_search("nothing", limit=10)
        assert results == []
        assert total == 0


class TestEnvVarsWiring:
    """P0c: env_vars boost repos when query contains UPPERCASE identifiers."""

    @patch("src.search.hybrid.code_facts_search", return_value=[])
    @patch("src.search.hybrid.env_var_search")
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search")
    def test_repo_boosted_when_env_var_matches(
        self, mock_fts, mock_vec, mock_rerank, mock_ev, mock_cf
    ):
        sr_match = _make_sr(1, repo="express-api-callbacks")
        sr_other = _make_sr(2, repo="other-repo")
        mock_fts.return_value = [sr_match, sr_other]
        mock_ev.return_value = [
            {
                "repo": "express-api-callbacks",
                "var_name": "FORCE_REDIRECTS_PROVIDERS",
                "raw_value": "skrill",
                "source": "consts_js_raw",
            }
        ]

        results, _err, _total = hybrid_search("set FORCE_REDIRECTS_PROVIDERS flag", limit=10)
        boosted = next(r for r in results if r["repo_name"] == "express-api-callbacks")
        other = next(r for r in results if r["repo_name"] == "other-repo")
        assert "env_var" in boosted["sources"]
        assert "env_var" not in other["sources"]
        assert boosted["score"] > other["score"]


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


class TestRerankPenalties:
    """P4.1: doc/test/guide chunks are down-weighted on code queries."""

    @patch("src.search.hybrid.get_reranker")
    def test_doc_file_type_penalized(self, mock_get_reranker):
        # Tied rerank scores — doc should fall behind code after penalty.
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "doc snippet", "repo_name": "r1",
             "file_path": "docs/docs/data-layer.md", "file_type": "doc"},
            {"score": 0.5, "snippet": "code snippet", "repo_name": "r2",
             "file_path": "libs/payout/handle.js", "file_type": "library"},
        ]
        result = rerank("payout handler code", items, limit=2)
        # Code wins — doc got DOC_PENALTY.
        assert result[0]["repo_name"] == "r2"
        assert result[0]["penalty"] == 0.0
        assert result[1]["penalty"] > 0

    @patch("src.search.hybrid.get_reranker")
    def test_spec_path_penalized(self, mock_get_reranker):
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "spec", "repo_name": "r1",
             "file_path": "libs/foo.spec.js", "file_type": "library"},
            {"score": 0.5, "snippet": "code", "repo_name": "r2",
             "file_path": "libs/foo.js", "file_type": "library"},
        ]
        result = rerank("policy handler", items, limit=2)
        assert result[0]["repo_name"] == "r2"
        assert result[1]["penalty"] > 0

    @patch("src.search.hybrid.get_reranker")
    def test_ai_coding_guide_strongest_penalty(self, mock_get_reranker):
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "guide", "repo_name": "r1",
             "file_path": "AI-CODING-GUIDE.md", "file_type": "doc"},
            {"score": 0.5, "snippet": "spec", "repo_name": "r2",
             "file_path": "foo.spec.js", "file_type": "library"},
            {"score": 0.5, "snippet": "code", "repo_name": "r3",
             "file_path": "libs/handler.js", "file_type": "library"},
        ]
        result = rerank("handler pattern", items, limit=3)
        assert result[0]["repo_name"] == "r3"  # code wins
        # Guide penalty >= spec penalty.
        guide = next(r for r in result if r["repo_name"] == "r1")
        spec = next(r for r in result if r["repo_name"] == "r2")
        assert guide["penalty"] >= spec["penalty"]

    @patch("src.search.hybrid.get_reranker")
    def test_penalty_skipped_when_query_asks_for_docs(self, mock_get_reranker):
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.9, 0.5]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "doc", "repo_name": "r1",
             "file_path": "README.md", "file_type": "doc"},
            {"score": 0.5, "snippet": "code", "repo_name": "r2",
             "file_path": "libs/foo.js", "file_type": "library"},
        ]
        result = rerank("how to docs for README guide", items, limit=2)
        # Query contains "docs" / "README" / "guide" — no penalty applied.
        for r in result:
            assert r["penalty"] == 0.0

    @patch("src.search.hybrid.get_reranker")
    def test_production_code_not_penalized(self, mock_get_reranker):
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.7, 0.7]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "handler", "repo_name": "r1",
             "file_path": "libs/webhooks/handle.js", "file_type": "library"},
            {"score": 0.5, "snippet": "method", "repo_name": "r2",
             "file_path": "src/methods/payout.js", "file_type": "grpc_method"},
        ]
        result = rerank("webhook handler", items, limit=2)
        for r in result:
            assert r["penalty"] == 0.0
