"""Unit tests for scripts/churn_replay metric helpers.

Focused on metric math — the model-running path is integration-tested by
executing the script against ≥2 real queries (smoke test, not in CI).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.analysis.churn_replay as cr  # type: ignore[import-not-found]  # noqa: E402


def _fake_result(repo: str, file_path: str) -> dict:
    return {"repo_name": repo, "file_path": file_path, "file_type": "code", "chunk_type": "code"}


def test_key_is_repo_and_file() -> None:
    r = _fake_result("backoffice-web", "src/Foo.tsx")
    assert cr._key(r) == "backoffice-web::src/Foo.tsx"


def test_top_keys_dedupes_within_same_file() -> None:
    results = [
        _fake_result("a", "x.ts"),
        _fake_result("a", "x.ts"),  # duplicate — sibling chunks collapse
        _fake_result("a", "y.ts"),
        _fake_result("b", "x.ts"),
    ]
    keys = cr._top_keys(results, 3)
    assert keys == ["a::x.ts", "a::y.ts", "b::x.ts"]


def test_top_keys_respects_k_after_dedup() -> None:
    results = [_fake_result("r", f"f{i}.py") for i in range(20)]
    assert len(cr._top_keys(results, 5)) == 5


def test_overlap_at_k_full_match() -> None:
    a = ["r::x", "r::y", "r::z"]
    b = ["r::x", "r::y", "r::z"]
    assert cr._overlap_at_k(a, b, 3) == 1.0


def test_overlap_at_k_no_match() -> None:
    a = ["r::x", "r::y"]
    b = ["s::x", "s::y"]
    assert cr._overlap_at_k(a, b, 2) == 0.0


def test_overlap_at_k_partial_order_insensitive() -> None:
    a = ["r::1", "r::2", "r::3"]
    b = ["r::3", "r::9", "r::1"]
    # intersection {r::1, r::3} / k=3 = 2/3
    assert round(cr._overlap_at_k(a, b, 3), 3) == round(2 / 3, 3)


def test_jaccard_at_k_symmetric() -> None:
    a = ["r::1", "r::2", "r::3"]
    b = ["r::2", "r::3", "r::4"]
    # intersection 2, union 4 → 0.5
    assert cr._jaccard_at_k(a, b, 3) == 0.5
    assert cr._jaccard_at_k(b, a, 3) == 0.5


def test_jaccard_at_k_zero_when_empty() -> None:
    assert cr._jaccard_at_k([], [], 3) == 0.0


def test_rank_diff_mean_moves() -> None:
    a = ["r::1", "r::2", "r::3"]  # r::2 is rank 2
    b = ["r::3", "r::2", "r::1"]  # r::2 is rank 2
    # shared = {r::1, r::2, r::3}: diffs = |1-3|, |2-2|, |3-1| = 2,0,2 → mean 4/3
    assert cr._rank_diff_mean(a, b, 3) == 4 / 3


def test_rank_diff_mean_no_shared_returns_none() -> None:
    a = ["r::1"]
    b = ["r::2"]
    assert cr._rank_diff_mean(a, b, 3) is None


def test_compute_metrics_top1_changed() -> None:
    base = [[_fake_result("a", f"f{i}.ts") for i in range(5)]]
    v8 = [[_fake_result("a", f"f{4 - i}.ts") for i in range(5)]]  # reversed
    out = cr._compute_metrics(base, v8, top_k=5)
    assert out[0]["top1_changed"] is True
    # Same 5 items, reversed order → overlap@1=0, jaccard@5 covers full set
    assert out[0]["overlap_at_k"][1] == 0.0
    assert out[0]["overlap_at_k"][5] == 1.0
    assert out[0]["jaccard_at_k"][5] == 1.0


def test_aggregate_basic_shape() -> None:
    per_query = [
        {
            "overlap_at_k": {1: 1.0, 3: 1.0, 5: 1.0, 10: 1.0},
            "jaccard_at_k": {3: 1.0, 5: 1.0, 10: 1.0},
            "top1_changed": False,
            "rank_diff_mean_at_10": 0.0,
        },
        {
            "overlap_at_k": {1: 0.0, 3: 0.33, 5: 0.2, 10: 0.3},
            "jaccard_at_k": {3: 0.2, 5: 0.1, 10: 0.176},
            "top1_changed": True,
            "rank_diff_mean_at_10": 4.5,
        },
    ]
    summary = cr._aggregate(per_query, top_k=10)
    assert summary["n_queries"] == 2
    assert summary["pct_top1_changed"] == 50.0
    assert summary["mean_overlap_at_k"][10] == round((1.0 + 0.3) / 2, 4)
    assert summary["pct_high_churn_at_10"] == 50.0  # second query < 0.5
    assert summary["mean_rank_diff_at_10"] == round((0.0 + 4.5) / 2, 3)
