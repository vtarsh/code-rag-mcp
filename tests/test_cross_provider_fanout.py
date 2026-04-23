"""Tests for cross-provider fan-out (hybrid.py + service.py).

Reformulation-agent finding (2026-04-23): 82% of reformulation chains end with
identical result_len (user searches in vain) and 56% of transitions are pure
provider-token swaps (nuvei -> payper -> volt). The `cross_provider=True` flag
short-circuits these chains by returning top-1 analogous chunks from sibling
provider repos in a single call.
"""

from unittest.mock import patch

import pytest

from src.search.hybrid import (
    _MAX_SIBLINGS,
    _cross_provider_fanout,
    _detect_provider_topic,
    _sibling_provider_repos,
    hybrid_search,
)
from src.types import SearchResult


@pytest.fixture(autouse=True)
def _mock_wiring():
    """Suppress code_facts/env_vars wiring so we don't hit the live DB."""
    with (
        patch("src.search.hybrid.code_facts_search", return_value=[]),
        patch("src.search.hybrid.env_var_search", return_value=[]),
    ):
        yield


def _make_sr(repo: str, path: str, snippet: str = "sibling chunk body") -> SearchResult:
    return SearchResult(
        rowid=1,
        repo_name=repo,
        file_path=path,
        file_type="grpc_method",
        chunk_type="function",
        snippet=snippet,
    )


class TestDetectProviderTopic:
    def test_provider_plus_topic_triggers(self):
        assert _detect_provider_topic("nuvei payout handle-activities.js") == ("nuvei", "payout")

    def test_reversed_order_still_triggers(self):
        # Order-independent — tokens are put in a set.
        assert _detect_provider_topic("payout nuvei") == ("nuvei", "payout")

    def test_non_provider_query_skipped(self):
        assert _detect_provider_topic("idempotency key pattern") is None

    def test_provider_without_topic_skipped(self):
        assert _detect_provider_topic("nuvei config") is None

    def test_topic_without_provider_skipped(self):
        assert _detect_provider_topic("payout handler logic") is None

    def test_empty_query(self):
        assert _detect_provider_topic("") is None

    def test_none_query(self):
        assert _detect_provider_topic(None) is None  # type: ignore[arg-type]

    def test_multiple_providers_picks_one(self):
        hit = _detect_provider_topic("nuvei payper payout")
        assert hit is not None
        assert hit[0] in {"nuvei", "payper"}
        assert hit[1] == "payout"

    def test_punctuation_tolerated(self):
        assert _detect_provider_topic("nuvei, payout.js") == ("nuvei", "payout")

    def test_case_insensitive(self):
        assert _detect_provider_topic("Nuvei PAYOUT") == ("nuvei", "payout")

    def test_substring_not_matched(self):
        # "nuveix" should NOT match "nuvei" — word-boundary tokenisation.
        assert _detect_provider_topic("nuveix payout") is None


class TestSiblingProviderRepos:
    def test_excludes_active_provider(self):
        siblings = _sibling_provider_repos("nuvei")
        sib_names = {s for s, _ in siblings}
        assert "nuvei" not in sib_names

    def test_respects_max_siblings(self):
        siblings = _sibling_provider_repos("nuvei")
        unique = {s for s, _ in siblings}
        assert len(unique) <= _MAX_SIBLINGS

    def test_uses_configured_prefixes(self):
        # With the default pay-com prefixes, the first repo should use grpc-apm-
        siblings = _sibling_provider_repos("nuvei")
        assert siblings, "expected at least one sibling"
        _, repo = siblings[0]
        assert repo.startswith(("grpc-apm-", "grpc-providers-"))


class TestCrossProviderFanout:
    @patch("src.search.hybrid.fts_search")
    def test_triggers_on_provider_topic(self, mock_fts):
        mock_fts.side_effect = lambda topic, repo="", limit=1: [
            _make_sr(repo, f"{repo}/methods/{topic}.js"),
        ]
        header, topic = _cross_provider_fanout("nuvei payout")
        assert topic == "payout"
        assert header is not None
        assert "Cross-provider siblings for 'payout'" in header

    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_skipped_on_non_provider_query(self, mock_fts):
        header, topic = _cross_provider_fanout("idempotency key pattern")
        assert header is None
        assert topic is None
        # fts_search was never called — detection short-circuits.
        mock_fts.assert_not_called()

    @patch("src.search.hybrid.fts_search")
    def test_six_sibling_limit_enforced(self, mock_fts):
        """Even if all 9 siblings return hits, the header includes at most 6."""
        mock_fts.side_effect = lambda topic, repo="", limit=1: [
            _make_sr(repo, f"{repo}/methods/{topic}.js"),
        ]
        header, _topic = _cross_provider_fanout("nuvei payout")
        assert header is not None
        # Each sibling contributes one "  - **repo**" line.
        sibling_lines = [ln for ln in header.splitlines() if ln.startswith("  - **")]
        assert len(sibling_lines) <= _MAX_SIBLINGS
        # On pay-com defaults (9 siblings available), we expect exactly MAX.
        assert len(sibling_lines) == _MAX_SIBLINGS

    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_no_sibling_hits_returns_none(self, mock_fts):
        header, topic = _cross_provider_fanout("nuvei payout")
        assert header is None
        assert topic is None


class TestHybridSearchCrossProvider:
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search")
    def test_default_false_preserves_output(self, mock_fts, mock_vec, mock_rerank):
        """Default cross_provider=False → snippet unchanged, no header injected."""
        mock_fts.return_value = [_make_sr("grpc-apm-nuvei", "methods/payout.js", snippet="ORIGINAL_BODY")]
        results, _err, _total = hybrid_search("nuvei payout")
        assert len(results) == 1
        # Snippet must not contain the cross-provider header.
        assert "Cross-provider siblings" not in results[0].get("snippet", "")
        # The sentinel is absent.
        assert "has_cross_provider" not in results[0]

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search")
    def test_cross_provider_true_prepends_header(self, mock_fts, mock_vec, mock_rerank):
        mock_fts.return_value = [_make_sr("grpc-apm-nuvei", "methods/payout.js", snippet="ORIGINAL_BODY")]
        results, _err, _total = hybrid_search("nuvei payout", cross_provider=True)
        assert len(results) == 1
        assert results[0].get("has_cross_provider") is True
        assert "Cross-provider siblings for 'payout'" in results[0]["snippet"]
        # Original body is preserved below the header.
        assert "ORIGINAL_BODY" in results[0]["snippet"]

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search")
    def test_cross_provider_true_non_provider_query_unchanged(self, mock_fts, mock_vec, mock_rerank):
        """Flag set but query doesn't match pattern → no header (no false-positive)."""
        mock_fts.return_value = [_make_sr("libs-common", "keys/idempotency.ts", snippet="ORIGINAL_BODY")]
        results, _err, _total = hybrid_search("idempotency key pattern", cross_provider=True)
        assert len(results) == 1
        assert "Cross-provider siblings" not in results[0]["snippet"]
        assert "has_cross_provider" not in results[0]


class TestSearchToolPlumbing:
    """service.py forwards the flag without touching cache key of legacy callers."""

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_flag_forwarded_to_hybrid(self, mock_hybrid, mock_cache, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = (
            [{
                "repo_name": "grpc-apm-nuvei",
                "file_path": "methods/payout.js",
                "file_type": "grpc_method",
                "chunk_type": "function",
                "snippet": "body",
                "sources": ["keyword"],
            }],
            None,
            1,
        )
        search_tool("nuvei payout", cross_provider=True)
        # Last call — verify keyword propagation.
        _, kwargs = mock_hybrid.call_args
        assert kwargs.get("cross_provider") is True

    @patch("src.container.check_db_health", return_value=None)
    @patch("src.search.service.cache_or_compute", side_effect=lambda _k, fn: fn())
    @patch("src.search.service.hybrid_search")
    def test_default_false_forwarded(self, mock_hybrid, mock_cache, mock_health):
        from src.search.service import search_tool

        mock_hybrid.return_value = ([], None, 0)
        search_tool("anything")
        _, kwargs = mock_hybrid.call_args
        assert kwargs.get("cross_provider") is False


class TestDaemonPlumbing:
    """daemon.py TOOLS['search'] lambda must pass cross_provider through."""

    def test_daemon_lambda_accepts_cross_provider(self):
        import daemon as daemon_mod

        with patch("daemon.search_tool") as mock_search:
            mock_search.return_value = "ok"
            daemon_mod.TOOLS["search"]({"query": "nuvei payout", "cross_provider": True})
            _, kwargs = mock_search.call_args
            assert kwargs.get("cross_provider") is True

    def test_daemon_lambda_default_cross_provider_false(self):
        import daemon as daemon_mod

        with patch("daemon.search_tool") as mock_search:
            mock_search.return_value = "ok"
            daemon_mod.TOOLS["search"]({"query": "anything"})
            _, kwargs = mock_search.call_args
            assert kwargs.get("cross_provider") is False
