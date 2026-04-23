"""Tests for two-tower vector-leg routing in `hybrid_search` (2026-04-23).

Agent B scope — complements the foundation tests in
`tests/test_two_tower_foundation.py` (models / provider / container wiring).

We mock `vector_search` inside `src.search.hybrid` so the tests don't need
LanceDB, an actual tower, or the embedding model. `fts_search` is stubbed out
so the RRF pool is dominated by the mocked vector leg — makes assertions on
pool composition trivial.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_wiring():
    """Match the pattern from tests/test_hybrid.py — suppress code_facts and
    env_vars wiring so the RRF pool stays deterministic across tests."""
    with (
        patch("src.search.hybrid.code_facts_search", return_value=[]),
        patch("src.search.hybrid.env_var_search", return_value=[]),
    ):
        yield


def _vr(rowid: int, repo: str = "repo-x", file_type: str = "library") -> dict:
    """Build a minimal vector-search result dict."""
    return {
        "rowid": rowid,
        "repo_name": repo,
        "file_path": f"src/{repo}/file.ts",
        "file_type": file_type,
        "chunk_type": "function",
        "content_preview": f"preview for rowid {rowid} in {repo}",
    }


def _model_keys_from_calls(mock_vs: MagicMock) -> list[object]:
    """Return the `model_key` values passed to each vector_search call.

    vector_search is called positionally from hybrid_search except for
    model_key, which is always a kwarg — so we read it from call_args.kwargs.
    Missing = None (legacy default / code tower).
    """
    return [c.kwargs.get("model_key") for c in mock_vs.call_args_list]


class TestRoutingByIntent:
    """`docs_index=None` (default) → route by `_query_wants_docs` + code signal."""

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_pure_doc_intent_queries_docs_tower_only(self, _mock_fts, mock_vs, _mock_rr):
        """Absence-heuristic doc-intent (no code sig, 2-15 tokens) → docs only."""
        from src.search.hybrid import hybrid_search

        mock_vs.return_value = ([_vr(1)], None)
        hybrid_search("provider response mapping reference")

        keys = _model_keys_from_calls(mock_vs)
        assert keys == ["docs"], f"expected only docs-tower call, got {keys}"

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_pure_code_intent_queries_code_tower_only(self, _mock_fts, mock_vs, _mock_rr):
        """Code signature (fn() call) + repo token → code tower only (model_key=None)."""
        from src.search.hybrid import hybrid_search

        mock_vs.return_value = ([_vr(1)], None)
        hybrid_search("grpc_apm_trustly PayoutHandler()")

        keys = _model_keys_from_calls(mock_vs)
        assert keys == [None], f"expected only code-tower call (None), got {keys}"

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_mixed_query_fans_out_to_both_towers(self, _mock_fts, mock_vs, _mock_rr):
        """Single-token query has no code sig AND fails the 2-15 token absence
        heuristic → auto-route falls through to the "both" branch.

        `reconciliation` is explicitly covered in test_hybrid_doc_intent as a
        single-token → not doc-intent case; combined with no code signal it
        lands in the ambiguous / mixed bucket here."""
        from src.search.hybrid import hybrid_search

        # Return disjoint rowid sets so the merged pool has 2 records and
        # neither tower wins by dedupe — exercises the merge path cleanly.
        def _side(query, repo, ft, ex, limit, model_key=None):
            if model_key == "docs":
                return ([_vr(200, repo="docs-repo")], None)
            return ([_vr(100, repo="code-repo")], None)

        mock_vs.side_effect = _side
        hybrid_search("reconciliation")

        keys = _model_keys_from_calls(mock_vs)
        assert sorted(str(k) for k in keys) == ["None", "docs"], f"expected both towers queried, got {keys}"
        assert len(keys) == 2


class TestDocsIndexOverride:
    """`docs_index=True/False` forces a tower regardless of intent."""

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_docs_index_true_overrides_code_intent(self, _mock_fts, mock_vs, _mock_rr):
        """Query has a code signature but operator forces the docs tower."""
        from src.search.hybrid import hybrid_search

        mock_vs.return_value = ([_vr(1)], None)
        # Query has fn() call → would normally route to code tower.
        hybrid_search("handleCallback(req)", docs_index=True)

        keys = _model_keys_from_calls(mock_vs)
        assert keys == ["docs"]

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_docs_index_false_overrides_doc_intent(self, _mock_fts, mock_vs, _mock_rr):
        """Query looks doc-intent but operator forces the code tower."""
        from src.search.hybrid import hybrid_search

        mock_vs.return_value = ([_vr(1)], None)
        # Query is absence-heuristic doc-intent → would normally route to docs.
        hybrid_search("provider response mapping reference", docs_index=False)

        keys = _model_keys_from_calls(mock_vs)
        assert keys == [None]


class TestMergeDedupe:
    """Mixed routing must not double-count chunks that surface in both towers."""

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_same_rowid_in_both_towers_counted_once(self, _mock_fts, mock_vs, _mock_rr):
        """If rowid=42 surfaces in both code and docs towers, the RRF loop
        must record it at a single `vec:42` key — no double boost."""
        from src.search.hybrid import hybrid_search

        # Both towers return the SAME rowid (same chunks row, just different
        # embedding spaces). Before dedupe the merged pool would contain two
        # entries and the RRF loop would accumulate both scores at vec:42.
        def _side(query, repo, ft, ex, limit, model_key=None):
            if model_key == "docs":
                return ([_vr(42, repo="docs-repo")], None)
            return ([_vr(42, repo="code-repo")], None)

        mock_vs.side_effect = _side
        results, _err, total = hybrid_search("reconciliation", limit=10)

        # Exactly one record for rowid=42. Code tower ran first, so the
        # code-repo metadata wins (by design of keep-first dedupe).
        assert total == 1, f"expected 1 deduped record, got {total}"
        assert len(results) == 1
        assert results[0]["repo_name"] == "code-repo"
        # Exactly one "vector" source tag — proves the RRF loop didn't fire
        # twice for the same rowid.
        assert results[0]["sources"].count("vector") == 1

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_distinct_rowids_across_towers_preserved(self, _mock_fts, mock_vs, _mock_rr):
        """Dedupe must not collapse distinct rowids — both should land in the pool."""
        from src.search.hybrid import hybrid_search

        def _side(query, repo, ft, ex, limit, model_key=None):
            if model_key == "docs":
                return ([_vr(999, repo="docs-repo")], None)
            return ([_vr(42, repo="code-repo")], None)

        mock_vs.side_effect = _side
        results, _err, total = hybrid_search("reconciliation", limit=10)

        assert total == 2
        repos = {r["repo_name"] for r in results}
        assert repos == {"code-repo", "docs-repo"}


class TestLegacyBehaviourPreserved:
    """Default `model_key=None` path must still resolve to the code tower."""

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search")
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_code_intent_uses_none_model_key(self, _mock_fts, mock_vs, _mock_rr):
        """Code-intent query must pass model_key=None (or omit it) to preserve
        pre-two-tower behaviour for callers that hit the code tower."""
        from src.search.hybrid import hybrid_search

        mock_vs.return_value = ([], None)
        hybrid_search("SIGTERM_HANDLER")  # SCREAMING_SNAKE → code sig

        # Exactly one call, and it must NOT have requested the docs tower.
        assert mock_vs.call_count == 1
        keys = _model_keys_from_calls(mock_vs)
        assert keys[0] is None
