"""Tests for the v1 pointwise reranker eval (Bug 2 fix).

Covers two surfaces:
1. The output file (`profiles/pay-com/rerank_pointwise_eval_v1.jsonl`) — schema +
   size acceptance bar (>=30 unique queries; median >=3 positives per query).
2. The builder helpers (`scripts/build_rerank_pointwise_eval.py`) — `stats()`
   computes the right per-query buckets given a synthetic pair list.

We deliberately do NOT exercise the FTS5 / SQLite / CrossEncoder paths — those
are integration territory and would require a live `db/knowledge.db`. The full
eval-build is gated by the live build itself; CI just locks the JSONL contract.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

EVAL_PATH = REPO_ROOT / "profiles" / "pay-com" / "rerank_pointwise_eval_v1.jsonl"

REQUIRED_FIELDS = ("query", "doc_path", "doc_text", "label", "query_id", "stratum")


# --- helper to import the script as a module --------------------------------


def _load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_rerank_pointwise_eval",
        REPO_ROOT / "scripts" / "build" / "build_rerank_pointwise_eval.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- Phase 1: the live JSONL ------------------------------------------------


@pytest.fixture(scope="module")
def pairs() -> list[dict]:
    if not EVAL_PATH.exists():
        pytest.skip(f"{EVAL_PATH} not built yet")
    rows: list[dict] = []
    with EVAL_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def test_eval_file_exists():
    assert EVAL_PATH.exists(), f"{EVAL_PATH} must be built before running this suite"


def test_jsonl_is_valid(pairs):
    """Every row decodes and is a dict — the build step never wrote a bad line."""
    assert pairs, "eval is empty"
    for r in pairs:
        assert isinstance(r, dict)


def test_schema_present(pairs):
    """Every row has the contract fields with the right primitive types."""
    for r in pairs:
        for key in REQUIRED_FIELDS:
            assert key in r, f"missing {key} in {r}"
        assert isinstance(r["query"], str)
        assert isinstance(r["doc_path"], str)
        assert isinstance(r["doc_text"], str)
        assert r["label"] in (0, 1)
        assert isinstance(r["query_id"], str)
        assert isinstance(r["stratum"], str)


def test_min_unique_queries(pairs):
    """Bug-2 acceptance: >= 30 unique query_ids."""
    qids = {r["query_id"] for r in pairs}
    assert len(qids) >= 30, f"only {len(qids)} unique query_ids, need >= 30"


def test_median_positives_per_query(pairs):
    """Bug-2 acceptance: median positives per query >= 3."""
    by_qid: dict[str, int] = defaultdict(int)
    for r in pairs:
        if r["label"] == 1:
            by_qid[r["query_id"]] += 1
    assert by_qid, "no positives at all"
    median_pos = statistics.median(list(by_qid.values()))
    assert median_pos >= 3, f"median positives={median_pos}, need >= 3"


def test_every_query_has_at_least_one_positive(pairs):
    """A query with zero positives is unscoreable — guard against that drift."""
    pos_qids: set[str] = set()
    all_qids: set[str] = set()
    for r in pairs:
        all_qids.add(r["query_id"])
        if r["label"] == 1:
            pos_qids.add(r["query_id"])
    assert all_qids == pos_qids, f"queries without positives: {all_qids - pos_qids}"


def test_doc_text_nonempty(pairs):
    """No row should be emitted with empty doc_text — the builder skips those."""
    empty = [r for r in pairs if not r["doc_text"]]
    assert not empty, f"{len(empty)} rows have empty doc_text"


# --- Phase 2: builder unit tests --------------------------------------------


def test_stats_counts_pos_and_neg_correctly():
    """stats() should bucket pairs per query_id and report median pos correctly."""
    builder = _load_builder()
    pairs = [
        {"query_id": "q1", "label": 1, "query": "x", "doc_path": "a", "doc_text": "t", "stratum": "s"},
        {"query_id": "q1", "label": 1, "query": "x", "doc_path": "b", "doc_text": "t", "stratum": "s"},
        {"query_id": "q1", "label": 0, "query": "x", "doc_path": "c", "doc_text": "t", "stratum": "s"},
        {"query_id": "q2", "label": 1, "query": "y", "doc_path": "a", "doc_text": "t", "stratum": "s"},
        {"query_id": "q2", "label": 1, "query": "y", "doc_path": "b", "doc_text": "t", "stratum": "s"},
        {"query_id": "q2", "label": 1, "query": "y", "doc_path": "c", "doc_text": "t", "stratum": "s"},
        {"query_id": "q2", "label": 0, "query": "y", "doc_path": "d", "doc_text": "t", "stratum": "s"},
    ]
    s = builder.stats(pairs)
    assert s["n_pairs"] == 7
    assert s["n_queries"] == 2
    assert s["total_pos"] == 5
    assert s["total_neg"] == 2
    # median of [2, 3] = 2.5
    assert s["median_pos_per_query"] == pytest.approx(2.5)


def test_recall_at_10_basic():
    """Recall@10 helper computes |top10 ∩ pos| / min(|pos|, 10) — sanity check."""
    builder = _load_builder()
    # All 3 positives in top-10 -> recall = 1.0
    r = builder._recall_at_10([0, 1, 2, 3, 4], {0, 2, 4})
    assert r == 1.0
    # No positives in top-10 -> 0.0
    r = builder._recall_at_10([5, 6, 7], {0, 2, 4})
    assert r == 0.0
    # 1 of 2 positives in top-10 -> 0.5
    r = builder._recall_at_10([0, 1, 2, 3], {0, 99})
    assert r == 0.5
