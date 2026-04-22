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
    with (
        patch("src.search.hybrid.code_facts_search", return_value=[]),
        patch("src.search.hybrid.env_var_search", return_value=[]),
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
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_empty_results(self, mock_fts, mock_vec, mock_rerank):
        results, err, total = hybrid_search("nonexistent query")
        assert results == []
        assert err is None
        assert total == 0

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search")
    def test_keyword_only(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(1), _make_sr(2)]
        results, err, total = hybrid_search("payment")
        assert len(results) == 2
        assert err is None
        assert total >= len(results)

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_vector_only(self, mock_fts, mock_vec, mock_rerank):
        mock_vec.return_value = ([_make_vr(10), _make_vr(11)], None)
        results, _err, total = hybrid_search("semantic query")
        assert len(results) == 2
        assert total == 2

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_vector_error_propagated(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(1)]
        mock_vec.return_value = ([], "LanceDB not found")
        _results, err, total = hybrid_search("payment")
        assert err == "LanceDB not found"
        assert total >= 1

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_rrf_fusion_merges_sources(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(42)]
        mock_vec.return_value = ([_make_vr(42)], None)
        results, _err, total = hybrid_search("overlapping")
        assert total == 1
        assert "keyword" in results[0]["sources"]
        assert "vector" in results[0]["sources"]

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_total_candidates_gte_results(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(i) for i in range(15)]
        mock_vec.return_value = ([_make_vr(i + 100) for i in range(10)], None)
        results, _err, total = hybrid_search("big query", limit=5)
        assert total == 25
        assert total >= len(results)

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search")
    def test_keyword_weight_higher(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr(1)]
        mock_vec.return_value = ([_make_vr(2)], None)
        results, _, _ = hybrid_search("test", limit=10)
        keyword_result = next(r for r in results if "keyword" in r["sources"])
        vector_result = next(r for r in results if "vector" in r["sources"] and "keyword" not in r["sources"])
        assert keyword_result["score"] > vector_result["score"]

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
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
        assert result == items

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
        assert result[0]["repo_name"] == "r1"
        assert "combined_score" in result[0]
        assert "rerank_score" in result[0]


class TestRerankPenalties:
    """P4.1: doc/test/guide chunks are down-weighted on code queries."""

    @patch("src.search.hybrid.get_reranker")
    def test_doc_file_type_penalized(self, mock_get_reranker):
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
        assert result[0]["repo_name"] == "r3"
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

    # P1c 2026-04-22: regression tests from Opus judge failures.

    @patch("src.search.hybrid.get_reranker")
    def test_ci_deploy_yml_penalized_on_code_query(self, mock_get_reranker):
        """P1c #2: 'ach provider service integration repo' misfired to ci/deploy.yml."""
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "ci yaml", "repo_name": "workflow-ach-init",
             "file_path": "ci/deploy.yml", "file_type": "config"},
            {"score": 0.5, "snippet": "ach code", "repo_name": "grpc-banks-crb",
             "file_path": "methods/ach-payment.js", "file_type": "grpc_method"},
        ]
        result = rerank("ach provider service integration repo", items, limit=2)
        assert result[0]["repo_name"] == "grpc-banks-crb"
        assert result[1]["penalty"] > 0

    @patch("src.search.hybrid.get_reranker")
    def test_k8s_github_workflows_penalized(self, mock_get_reranker):
        """P1c: k8s/.github/workflows/* is also CI noise, not service code."""
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "gha", "repo_name": "workflow-ach-init",
             "file_path": "k8s/.github/workflows/deploy.yml", "file_type": "config"},
            {"score": 0.5, "snippet": "service", "repo_name": "grpc-apm-ach",
             "file_path": "methods/sale.js", "file_type": "grpc_method"},
        ]
        result = rerank("ach service code", items, limit=2)
        assert result[0]["repo_name"] == "grpc-apm-ach"
        assert result[1]["penalty"] > 0

    @patch("src.search.hybrid.get_reranker")
    def test_checklist_query_disables_penalty(self, mock_get_reranker):
        """P1c #5/#10/#42: 'checklist' token in query preserves docs/references."""
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "checklist", "repo_name": "provider-integration-checklist",
             "file_path": "docs/references/provider-integration-checklist.md",
             "file_type": "reference"},
            {"score": 0.5, "snippet": "init", "repo_name": "grpc-apm-ppro",
             "file_path": "methods/initialize.js", "file_type": "grpc_method"},
        ]
        result = rerank("new APM provider integration recipe checklist boilerplate", items, limit=2)
        for r in result:
            assert r["penalty"] == 0.0

    @patch("src.search.hybrid.get_reranker")
    def test_framework_query_disables_penalty(self, mock_get_reranker):
        """P1c #19: 'investigation framework' is a named doc artifact."""
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "framework", "repo_name": "investigation-framework",
             "file_path": "docs/references/investigation-framework.md", "file_type": "reference"},
            {"score": 0.5, "snippet": "proto", "repo_name": "libs-types",
             "file_path": "proto/protos/onboarding.proto", "file_type": "library"},
        ]
        result = rerank("investigation framework", items, limit=2)
        for r in result:
            assert r["penalty"] == 0.0

    @patch("src.search.hybrid.get_reranker")
    def test_severity_rules_query_disables_penalty(self, mock_get_reranker):
        """P1c #4/#45: 'impact audit severity verification' + 'impact audit rules'."""
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "rules", "repo_name": "impact-audit-rules",
             "file_path": "docs/references/impact-audit-rules.md", "file_type": "reference"},
            {"score": 0.5, "snippet": "code", "repo_name": "graphql",
             "file_path": "src/resolvers/audit/queries/audits.ts", "file_type": "library"},
        ]
        result = rerank("impact audit severity verification", items, limit=2)
        for r in result:
            assert r["penalty"] == 0.0

    @patch("src.search.hybrid.get_reranker")
    def test_matrix_reference_sandbox_overview_disable_penalty(self, mock_get_reranker):
        """P1c #37/#38: 'reference matrix', 'sandbox testing' name doc artifacts."""
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        for q in [
            "provider reference matrix volt paysafe paynearme",
            "payper sandbox testing amount magic values test",
            "openfinance APM integration overview",
        ]:
            items = [
                {"score": 0.5, "snippet": "doc", "repo_name": "r1",
                 "file_path": "docs/references/foo.md", "file_type": "reference"},
                {"score": 0.5, "snippet": "code", "repo_name": "r2",
                 "file_path": "libs/bar.js", "file_type": "library"},
            ]
            result = rerank(q, items, limit=2)
            for r in result:
                assert r["penalty"] == 0.0, f"query {q!r} should disable penalty"

    @patch("src.search.hybrid.get_reranker")
    def test_ci_path_exempted_when_query_asks_docs(self, mock_get_reranker):
        """P1c: CI yml penalty respects the DOC_QUERY regex like other penalties."""
        mock_provider = MagicMock()
        mock_provider.rerank.return_value = [0.8, 0.8]
        mock_get_reranker.return_value = (mock_provider, None)
        items = [
            {"score": 0.5, "snippet": "ci", "repo_name": "workflow-ach-init",
             "file_path": "ci/deploy.yml", "file_type": "config"},
            {"score": 0.5, "snippet": "code", "repo_name": "grpc-apm-ach",
             "file_path": "methods/sale.js", "file_type": "grpc_method"},
        ]
        result = rerank("deploy documentation sandbox", items, limit=2)
        for r in result:
            assert r["penalty"] == 0.0
