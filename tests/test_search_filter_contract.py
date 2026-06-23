"""Regression tests for the file_type filter contract in hybrid_search.

Covers the 2026-06-23 fix for the bug where `search(file_type='gotchas')` on a
non-doc-phrased query returned code tagged [code_facts] instead of the requested
gotcha docs. Two compounding defects:

  1. `_DEMOTE_DOC_NOISE` (FIX-A) folded `gotchas`/`reference`/... into the exclude
     list whenever the query lacked a doc-trigger word — even when the caller
     EXPLICITLY asked for that type via `file_type=`. The resulting SQL
     `file_type='gotchas' AND file_type NOT IN ('gotchas', ...)` matched nothing.
  2. `_apply_code_facts` injected code-only candidates into the (now empty) pool
     without honouring file_type / exclude_file_types, so the void filled with
     code.

See src/search/hybrid.py.
"""

from __future__ import annotations

from unittest.mock import patch

from src.search.hybrid import _apply_code_facts, hybrid_search

# A query with NO doc-trigger word (test/docs/guide/gotcha/how-to/overview/...),
# so _DEMOTE_DOC_NOISE fires and would add gotchas to the exclude list.
_NON_DOC_QUERY = "error code mapping issuer response iso 8583 fallback"


# --------------------------------------------------------------------------- #
# Fix 1: an explicit file_type include must win over the auto doc-noise exclude
# --------------------------------------------------------------------------- #
class TestExplicitFileTypeWinsOverExclude:
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_requested_gotchas_not_in_exclude(self, mock_fts, _mock_vec, _mock_rerank):
        hybrid_search(_NON_DOC_QUERY, file_type="gotchas")
        # fts_search(query, repo, file_type, exclude_file_types, limit=...)
        args = mock_fts.call_args.args
        assert args[2] == "gotchas", "include filter must be passed through"
        exclude = args[3] or ""
        assert "gotchas" not in exclude.split(","), (
            f"explicit file_type='gotchas' must be dropped from exclude, got {exclude!r}"
        )

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_other_doc_noise_types_still_excluded(self, mock_fts, _mock_vec, _mock_rerank):
        # Asking for gotchas drops ONLY gotchas from exclude; sibling doc-noise
        # types (reference, provider_doc, ...) stay excluded as before.
        hybrid_search(_NON_DOC_QUERY, file_type="gotchas")
        exclude = (mock_fts.call_args.args[3] or "").split(",")
        assert "reference" in exclude
        assert "provider_doc" in exclude

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_no_filter_query_unchanged(self, mock_fts, _mock_vec, _mock_rerank):
        # Without an explicit file_type, the doc-noise exclude is untouched
        # (this is the n=665-benchmarked path — must stay byte-identical).
        hybrid_search(_NON_DOC_QUERY)
        exclude = (mock_fts.call_args.args[3] or "").split(",")
        assert "gotchas" in exclude  # demotion still active for code queries


# --------------------------------------------------------------------------- #
# Fix 2: code_facts injection must honour the same filter contract
# --------------------------------------------------------------------------- #
def _lib_chunk():
    return [
        {
            "rowid": 1,
            "repo_name": "r",
            "file_path": "f.js",
            "file_type": "library",
            "chunk_type": "code_file",
            "snippet": "x",
        }
    ]


class TestCodeFactsRespectsFilter:
    def test_include_filter_drops_mismatched_injection(self):
        scores: dict = {}
        with (
            patch("src.search.hybrid.code_facts_search", return_value=[{"repo_name": "r", "file_path": "f.js"}]),
            patch("src.search.hybrid.fetch_chunks_for_files", return_value=_lib_chunk()),
        ):
            _apply_code_facts(scores, "q", "", 60, 2.0, file_type="gotchas")
        assert scores == {}, "library injection must be dropped when file_type='gotchas'"

    def test_exclude_filter_drops_injection(self):
        scores: dict = {}
        with (
            patch("src.search.hybrid.code_facts_search", return_value=[{"repo_name": "r", "file_path": "f.js"}]),
            patch("src.search.hybrid.fetch_chunks_for_files", return_value=_lib_chunk()),
        ):
            _apply_code_facts(scores, "q", "", 60, 2.0, exclude_file_types="library")
        assert scores == {}, "library injection must be dropped when exclude=library"

    def test_no_filter_injects_as_before(self):
        # Negative control: with no filter, code_facts injects (unchanged behaviour).
        scores: dict = {}
        with (
            patch("src.search.hybrid.code_facts_search", return_value=[{"repo_name": "r", "file_path": "f.js"}]),
            patch("src.search.hybrid.fetch_chunks_for_files", return_value=_lib_chunk()),
        ):
            _apply_code_facts(scores, "q", "", 60, 2.0)
        assert "fts:1" in scores
        assert scores["fts:1"]["sources"] == ["code_facts"]

    def test_matching_include_filter_keeps_injection(self):
        # file_type='library' with a library injection → kept.
        scores: dict = {}
        with (
            patch("src.search.hybrid.code_facts_search", return_value=[{"repo_name": "r", "file_path": "f.js"}]),
            patch("src.search.hybrid.fetch_chunks_for_files", return_value=_lib_chunk()),
        ):
            _apply_code_facts(scores, "q", "", 60, 2.0, file_type="library")
        assert "fts:1" in scores


# --------------------------------------------------------------------------- #
# Fix 1: a caller's OWN exclude survives — only the requested include is dropped
# --------------------------------------------------------------------------- #
class TestCallerExcludePreserved:
    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_include_dropped_caller_exclude_kept(self, mock_fts, _mock_vec, _mock_rerank):
        # file_type='gotchas' must be removed from exclude, but a caller-supplied
        # 'library' exclude (NOT a _DOC_NOISE_TYPES member) must survive — proves
        # the include comprehension does not clobber the caller's own excludes.
        hybrid_search(_NON_DOC_QUERY, file_type="gotchas", exclude_file_types="library")
        exclude = (mock_fts.call_args.args[3] or "").split(",")
        assert "gotchas" not in exclude
        assert "library" in exclude

    @patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
    @patch("src.search.hybrid.vector_search", return_value=([], None))
    @patch("src.search.hybrid.fts_search", return_value=[])
    def test_reference_include_also_wins(self, mock_fts, _mock_vec, _mock_rerank):
        # The bug class is not gotchas-specific: 'reference' (another curated
        # doc-noise type) must likewise win over the auto exclude.
        hybrid_search(_NON_DOC_QUERY, file_type="reference")
        exclude = (mock_fts.call_args.args[3] or "").split(",")
        assert "reference" not in exclude
        assert "gotchas" in exclude  # siblings still excluded


# --------------------------------------------------------------------------- #
# Fix 2 load-bearing invariant: code_facts maps ONLY to code-type chunks.
# Fix 2's include filter assumes this — if a future reindex makes code_facts map
# to a doc-type chunk, the filter would silently drop legit injections AND
# bench-invariance no longer holds. Guard it from the DB so the violation fails
# loudly instead of silently shifting recall.
# --------------------------------------------------------------------------- #
def test_code_facts_maps_only_to_code_types():
    import sqlite3

    import pytest

    from src.config import DB_PATH

    if not DB_PATH.exists():
        pytest.skip("knowledge.db not present")
    doc_types = (
        "gotchas",
        "reference",
        "docs",
        "provider_doc",
        "dictionary",
        "domain_registry",
        "package_usage",
        "task",
        "flow_annotation",
    )
    conn = sqlite3.connect(str(DB_PATH))
    try:
        if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='code_facts'").fetchone():
            pytest.skip("code_facts table not present")
        placeholders = ",".join("?" * len(doc_types))
        n = conn.execute(
            f"""SELECT COUNT(*) FROM code_facts cf
                JOIN chunks c ON c.repo_name = cf.repo_name AND c.file_path = cf.file_path
                WHERE c.file_type IN ({placeholders})""",
            doc_types,
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 0, (
        f"{n} code_facts pairs map to doc-type chunks — Fix 2's include filter "
        "would silently drop legit code injections; revisit _apply_code_facts"
    )
