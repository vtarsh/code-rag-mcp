"""Unit tests for fts_search_per_token — Step 3 rare-token rescue."""

from unittest.mock import patch

from src.search.fts import fts_search_per_token
from src.types import SearchResult


def _sr(rowid: int, repo: str = "r1", path: str = "f.ts") -> SearchResult:
    return SearchResult(
        rowid=rowid,
        repo_name=repo,
        file_path=path,
        file_type="code",
        chunk_type="function",
        snippet="...",
    )


def test_empty_query_returns_empty():
    assert fts_search_per_token("") == []
    assert fts_search_per_token("   ") == []


def test_stopword_only_query_returns_empty():
    # "add the from" — all in _FTS_STOPWORDS
    with patch("src.search.fts.fts_search") as mock_fts:
        out = fts_search_per_token("add the from")
        assert out == []
        mock_fts.assert_not_called()


def test_short_tokens_dropped():
    # "a be by" — <3 chars or stopword
    with patch("src.search.fts.fts_search") as mock_fts:
        out = fts_search_per_token("a be by")
        assert out == []
        mock_fts.assert_not_called()


def test_per_token_calls_fts_for_each_content_token():
    # "merchantPricing rangeItem" — 2 content tokens
    calls = []

    def fake(query, repo="", file_type="", exclude_file_types="", limit=10):
        calls.append(query)
        return [_sr(rowid=hash(query) & 0xFF, path=f"{query}.ts")]

    with patch("src.search.fts.fts_search", side_effect=fake):
        out = fts_search_per_token("merchantPricing rangeItem")
        assert len(calls) == 2
        # Sorted by length desc — "merchantPricing"(15) > "rangeItem"(9)
        assert calls[0] == "merchantPricing"
        assert calls[1] == "rangeItem"
        assert len(out) == 2


def test_max_tokens_cap():
    # 12 tokens, cap at max_tokens=3 → only 3 FTS calls
    calls = []

    def fake(query, repo="", file_type="", exclude_file_types="", limit=10):
        calls.append(query)
        return []

    with patch("src.search.fts.fts_search", side_effect=fake):
        fts_search_per_token(
            "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu",
            max_tokens=3,
        )
        assert len(calls) == 3


def test_dedup_case_insensitive():
    # Same token in different case — should call FTS only once.
    calls = []

    def fake(query, repo="", file_type="", exclude_file_types="", limit=10):
        calls.append(query)
        return []

    with patch("src.search.fts.fts_search", side_effect=fake):
        fts_search_per_token("Merchant merchant MERCHANT")
        assert len(calls) == 1


def test_union_dedups_by_rowid_keeping_best_rank():
    # Two tokens: tok1 returns [rowid=1 at rank 0, rowid=2 at rank 1]
    # tok2 returns [rowid=2 at rank 0, rowid=3 at rank 1]
    # Expected: rowid=2 keeps rank 0 (best across tokens)
    def fake(query, repo="", file_type="", exclude_file_types="", limit=10):
        if query == "alphaToken":
            return [_sr(1, path="a.ts"), _sr(2, path="b.ts")]
        return [_sr(2, path="b.ts"), _sr(3, path="c.ts")]

    with patch("src.search.fts.fts_search", side_effect=fake):
        out = fts_search_per_token("alphaToken bravo")
        # First by best-rank: rowid 1 (rank 0 from tok1), rowid 2 (rank 0 from tok2), rowid 3 (rank 1 from tok2)
        ids = [sr.rowid for sr in out]
        assert ids[0] in (1, 2)  # both at rank 0
        assert 3 in ids  # rowid 3 included
        assert len(ids) == 3
        assert len(set(ids)) == 3  # deduped


def test_hyphenated_token_preserved_in_call():
    # _sanitize_fts_input does NOT split on hyphen; downstream sanitize_fts_query handles it
    calls = []

    def fake(query, repo="", file_type="", exclude_file_types="", limit=10):
        calls.append(query)
        return []

    with patch("src.search.fts.fts_search", side_effect=fake):
        fts_search_per_token("request-logs payment")
        # Tokens: "request-logs" (12), "payment" (7)
        assert "request-logs" in calls
        assert "payment" in calls


def test_per_token_limit_propagated():
    calls = []

    def fake(query, repo="", file_type="", exclude_file_types="", limit=10):
        calls.append((query, limit))
        return []

    with patch("src.search.fts.fts_search", side_effect=fake):
        fts_search_per_token("merchantPricing rangeItem", per_token_limit=25)
        for _, lim in calls:
            assert lim == 25


def test_repo_filter_propagated():
    calls = []

    def fake(query, repo="", file_type="", exclude_file_types="", limit=10):
        calls.append((query, repo))
        return []

    with patch("src.search.fts.fts_search", side_effect=fake):
        fts_search_per_token("merchantPricing", repo="backoffice-web")
        for _, rep in calls:
            assert rep == "backoffice-web"
