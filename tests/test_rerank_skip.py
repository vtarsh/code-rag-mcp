"""Tests for the P10 doc-intent reranker skip gate (Phase A2 — stratum-gated).

The gate at `src/search/hybrid.py:_should_skip_rerank()` decides per query
whether to skip the CrossEncoder reranker on doc-intent. Decision matrix:

  - non-doc-intent query                            → run reranker
  - env CODE_RAG_DOC_RERANK_OFF=1 (kill-switch)     → skip on all doc-intent
  - doc-intent + stratum in OFF set                 → skip (reranker hurts)
  - doc-intent + stratum in KEEP set                → run (reranker rescues)
  - doc-intent + unknown stratum                    → run (conservative)

A2-revise (2026-04-25 late): inverted per v2 LLM-calibrated eval (10 Opus
agents, ~2200 judgments, n=192 across 10 strata). Per-stratum R@10 deltas
(skip vs full rerank-on) on `doc_intent_eval_v3_n200_v2.jsonl`:

OFF strata — reranker HURTS, skip wins:
  webhook +3.35pp, trustly +2.68pp, method +1.30pp, payout +1.11pp

KEEP strata — reranker HELPS, must keep:
  nuvei -7.58pp, aircash -8.78pp, refund -14.51pp, interac 0.00,
  provider -0.24pp

`tail` (catch-all, no stratum tokens) → falls through to the conservative
KEEP-rerank default (also a small loss -1.96pp without rerank).

The kill-switch env var stays for back-compat / emergency.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.search.hybrid import (
    _DOC_RERANK_KEEP_STRATA,
    _DOC_RERANK_OFF_STRATA,
    _detect_stratum,
    _should_skip_rerank,
    hybrid_search,
)
from src.types import SearchResult


def _make_sr(rowid: int, repo: str = "repo-a", path: str | None = None) -> SearchResult:
    return SearchResult(
        rowid=rowid,
        repo_name=repo,
        file_path=path or f"src/{repo}/file.ts",
        file_type="grpc_method",
        chunk_type="function",
        snippet="snippet body",
    )


def _make_doc_sr(rowid: int, repo: str = "docs-repo") -> SearchResult:
    return SearchResult(
        rowid=rowid,
        repo_name=repo,
        file_path=f"docs/{repo}/guide.md",
        file_type="docs",
        chunk_type="section",
        snippet="how to configure idempotency tokens",
    )


# Doc-intent query with NO stratum token — stratum gate falls through to the
# conservative default (run reranker). Cross-checked against
# tests/test_hybrid_doc_intent.py (`_query_wants_docs` returns True via the
# absence-heuristic 2..15-token branch).
_DOC_QUERY_NO_STRATUM = "how to configure idempotency"

# Doc-intent query with a stratum token from the OFF set — gate skips rerank.
# `webhook` is OFF (reranker hurts on this stratum per A2-revise eval).
_DOC_QUERY_OFF_STRATUM = "webhook signature validation"

# Doc-intent query with a stratum token from the KEEP set — gate runs rerank.
# `nuvei` is KEEP (reranker helps on this stratum per A2-revise eval).
_DOC_QUERY_KEEP_STRATUM = "nuvei integration overview"

# Code-intent query (file extension + camelCase fn signal).
_CODE_QUERY = "handler.ts handleCallback(req)"


# ---------------------------------------------------------------------------
# Pure-function unit tests on the gate itself (no hybrid_search wiring).
# ---------------------------------------------------------------------------


def test_off_and_keep_sets_disjoint():
    """Sanity: OFF and KEEP must not overlap — ambiguous strata are a config bug."""
    assert _DOC_RERANK_OFF_STRATA.isdisjoint(_DOC_RERANK_KEEP_STRATA)


def test_detect_stratum_off_set():
    """OFF strata tokens map to their stratum (A2-revise: webhook/trustly/method/payout)."""
    assert _detect_stratum("trustly callback signature") == "trustly"
    assert _detect_stratum("webhook validation") == "webhook"
    assert _detect_stratum("notification handler design") == "webhook"
    assert _detect_stratum("payment method extractor") == "method"
    assert _detect_stratum("aptpay payout addUPO") == "payout"


def test_detect_stratum_keep_set():
    """KEEP strata tokens map to their stratum (A2-revise: nuvei/aircash/refund/interac/provider)."""
    assert _detect_stratum("nuvei integration overview") == "nuvei"
    assert _detect_stratum("aircash deposit example") == "aircash"
    assert _detect_stratum("refund flow examples") == "refund"
    assert _detect_stratum("chargeback policy") == "refund"
    assert _detect_stratum("payper interac etransfer") == "interac"
    assert _detect_stratum("e-transfer settlement") == "interac"
    assert _detect_stratum("psp integration overview") == "provider"


def test_detect_stratum_off_wins_over_keep():
    """When both OFF and KEEP tokens appear, OFF stratum wins (checked first)."""
    # `trustly` (OFF) + `provider` (KEEP) → OFF wins.
    assert _detect_stratum("trustly provider docs") == "trustly"
    # `webhook` (OFF) + `nuvei` (KEEP) → OFF wins (webhook checked first).
    assert _detect_stratum("nuvei webhook callback") == "webhook"
    # `payout` (OFF) + `refund` (KEEP) → OFF wins.
    assert _detect_stratum("payout refund schedule") == "payout"


def test_detect_stratum_case_insensitive():
    """Stratum tokens match regardless of case."""
    assert _detect_stratum("TruStly callback") == "trustly"
    assert _detect_stratum("WebHook signing") == "webhook"
    assert _detect_stratum("NuVei integration") == "nuvei"
    assert _detect_stratum("InterAC etransfer") == "interac"


def test_detect_stratum_unknown_returns_none():
    """Queries without any stratum token return None (conservative fallback)."""
    assert _detect_stratum("deposit example") is None
    assert _detect_stratum("how to configure idempotency") is None
    assert _detect_stratum("") is None


@pytest.mark.parametrize(
    "case_id,query,is_doc_intent,env_off,expected_skip",
    [
        # OFF strata + doc-intent → skip reranker
        ("off-webhook", "webhook signature validation", True, None, True),
        ("off-trustly", "trustly callback handler", True, None, True),
        ("off-payment-method", "payment method extractor", True, None, True),
        ("off-payout", "aptpay payout schedule", True, None, True),
        # KEEP strata + doc-intent → run reranker
        ("keep-nuvei", "nuvei integration overview", True, None, False),
        ("keep-aircash", "aircash deposit example", True, None, False),
        ("keep-refund", "refund flow examples", True, None, False),
        ("keep-interac", "payper interac etransfer", True, None, False),
        ("keep-provider", "psp integration overview", True, None, False),
        # Unknown stratum + doc-intent → conservative default = run
        ("unknown", "how to configure idempotency", True, None, False),
        # Code-intent always runs reranker even with stratum tokens
        ("code-intent-overrides-off", "webhook validation", False, None, False),
        # env=1 kill-switch forces skip on ALL doc-intent queries
        ("envkill-keep", "nuvei integration overview", True, "1", True),
        ("envkill-unknown", "how to configure idempotency", True, "1", True),
        # env=1 has no effect on code-intent queries
        ("envkill-no-code", "handler.ts handleCallback(req)", False, "1", False),
        # env='0' / non-'1' → kill-switch off; per-stratum gate active
        ("env0-keep", "nuvei integration overview", True, "0", False),
        ("env0-unknown", "how to configure idempotency", True, "0", False),
        ("env0-off-still-skips", "webhook signature validation", True, "0", True),
    ],
)
def test_should_skip_rerank(monkeypatch, case_id, query, is_doc_intent, env_off, expected_skip):
    """Comprehensive gate logic for `_should_skip_rerank`.

    Covers per-stratum gating (OFF/KEEP/unknown), code-intent override, and
    the `CODE_RAG_DOC_RERANK_OFF` kill-switch (env='1' only — '0' is a no-op).
    """
    if env_off is None:
        monkeypatch.delenv("CODE_RAG_DOC_RERANK_OFF", raising=False)
    else:
        monkeypatch.setenv("CODE_RAG_DOC_RERANK_OFF", env_off)
    assert _should_skip_rerank(query, is_doc_intent=is_doc_intent) is expected_skip


# ---------------------------------------------------------------------------
# End-to-end tests through hybrid_search() — verify rerank() is/isn't called.
# ---------------------------------------------------------------------------


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_rerank_skipped_for_off_stratum_doc_query(mock_fts, mock_vec, mock_rerank, monkeypatch, caplog):
    """Doc-intent + OFF stratum (webhook) → rerank() NOT called, telemetry log emitted."""
    monkeypatch.delenv("CODE_RAG_DOC_RERANK_OFF", raising=False)
    mock_fts.return_value = [_make_doc_sr(1), _make_doc_sr(2), _make_doc_sr(3)]

    with caplog.at_level("INFO", logger="src.search.hybrid"):
        results, _err, _total = hybrid_search(_DOC_QUERY_OFF_STRATUM, limit=10)

    mock_rerank.assert_not_called()
    assert any("rerank_skipped" in rec.message and "webhook" in rec.message for rec in caplog.records), (
        f"expected stratum=webhook in log, got: {[r.message for r in caplog.records]}"
    )
    assert len(results) > 0
    for r in results:
        assert r.get("rerank_score") == 0.0
        assert r.get("penalty") == 0.0
        assert r.get("combined_score") == r.get("score")


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_rerank_runs_for_keep_stratum_doc_query(mock_fts, mock_vec, mock_rerank, monkeypatch):
    """Doc-intent + KEEP stratum (nuvei) → rerank() runs (real wins preserved)."""
    monkeypatch.delenv("CODE_RAG_DOC_RERANK_OFF", raising=False)
    mock_fts.return_value = [_make_doc_sr(1), _make_doc_sr(2)]

    hybrid_search(_DOC_QUERY_KEEP_STRATUM, limit=10)

    mock_rerank.assert_called_once()


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_rerank_runs_for_unknown_stratum_doc_query(mock_fts, mock_vec, mock_rerank, monkeypatch):
    """Doc-intent + no stratum token → rerank() runs (conservative default)."""
    monkeypatch.delenv("CODE_RAG_DOC_RERANK_OFF", raising=False)
    mock_fts.return_value = [_make_doc_sr(1), _make_doc_sr(2)]

    hybrid_search(_DOC_QUERY_NO_STRATUM, limit=10)

    mock_rerank.assert_called_once()


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_rerank_runs_for_code_intent_with_off_stratum_token(mock_fts, mock_vec, mock_rerank, monkeypatch):
    """Code-intent query containing OFF stratum tokens → rerank() still runs."""
    monkeypatch.delenv("CODE_RAG_DOC_RERANK_OFF", raising=False)
    mock_fts.return_value = [_make_sr(1), _make_sr(2)]

    # Code signature wins over the `webhook` token in `_query_wants_docs`.
    hybrid_search("webhook.ts handleWebhookCallback(req)", limit=10)

    mock_rerank.assert_called_once()


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_kill_switch_overrides_keep_stratum(mock_fts, mock_vec, mock_rerank, monkeypatch, caplog):
    """env=1 + KEEP stratum doc-query → rerank still skipped (kill-switch wins)."""
    monkeypatch.setenv("CODE_RAG_DOC_RERANK_OFF", "1")
    mock_fts.return_value = [_make_doc_sr(1), _make_doc_sr(2)]

    with caplog.at_level("INFO", logger="src.search.hybrid"):
        hybrid_search(_DOC_QUERY_KEEP_STRATUM, limit=10)

    mock_rerank.assert_not_called()
    # Telemetry should still record the skip (stratum=nuvei is reported).
    assert any("rerank_skipped" in rec.message for rec in caplog.records)


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_kill_switch_does_not_affect_code_intent(mock_fts, mock_vec, mock_rerank, monkeypatch):
    """env=1 + code-intent → rerank() runs (env var only affects doc-intent)."""
    monkeypatch.setenv("CODE_RAG_DOC_RERANK_OFF", "1")
    mock_fts.return_value = [_make_sr(1), _make_sr(2)]

    hybrid_search(_CODE_QUERY, limit=10)

    mock_rerank.assert_called_once()


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_env_zero_keeps_per_stratum_gate_active(mock_fts, mock_vec, mock_rerank, monkeypatch):
    """env='0' is the same as unset — per-stratum gate still skips OFF strata."""
    monkeypatch.setenv("CODE_RAG_DOC_RERANK_OFF", "0")
    mock_fts.return_value = [_make_doc_sr(1), _make_doc_sr(2)]

    hybrid_search(_DOC_QUERY_OFF_STRATUM, limit=10)

    mock_rerank.assert_not_called()


@patch("src.search.hybrid.rerank", side_effect=lambda q, r, lim, **_kw: r[:lim])
@patch("src.search.hybrid.vector_search", return_value=([], None))
@patch("src.search.hybrid.fts_search")
def test_stratum_token_case_insensitive_through_hybrid_search(mock_fts, mock_vec, mock_rerank, monkeypatch):
    """Mixed-case stratum tokens (e.g., WebHook) trigger the same gate decision."""
    monkeypatch.delenv("CODE_RAG_DOC_RERANK_OFF", raising=False)
    mock_fts.return_value = [_make_doc_sr(1), _make_doc_sr(2)]

    hybrid_search("WebHook signing overview", limit=10)

    mock_rerank.assert_not_called()
