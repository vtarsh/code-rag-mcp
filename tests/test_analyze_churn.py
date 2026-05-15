"""Unit tests for scripts/analyze_churn classifier + slicer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_churn as ac  # type: ignore[import-not-found]  # noqa: E402


def _fake_result(repo: str, file_path: str) -> dict:
    return {"repo_name": repo, "file_path": file_path, "file_type": "code", "chunk_type": "code"}


def test_classify_short_query() -> None:
    c = ac._classify("payout method")
    assert c["length_bucket"] == "short"
    assert c["n_tokens"] == 2
    assert c["has_uppercase_id"] is False
    assert c["has_jira_prefix"] is False


def test_classify_uppercase_identifier_detected() -> None:
    c = ac._classify("ACTIVATE_EXPIRE_SESSION flag")
    assert c["has_uppercase_id"] is True


def test_classify_single_letter_uppercase_not_detected() -> None:
    # "A" alone shouldn't fire (needs 3+ chars).
    c = ac._classify("query with A in middle")
    assert c["has_uppercase_id"] is False


def test_classify_jira_prefix() -> None:
    c = ac._classify("BO-798 backoffice issue")
    assert c["has_jira_prefix"] is True
    # "BO" is only 2 chars; the uppercase_id regex requires 3+, so these don't fire.
    assert c["has_uppercase_id"] is False


def test_classify_jira_prefix_hs_core() -> None:
    for q in ("HS-284 hosted session", "CORE-2348 something", "PI-57 delay"):
        assert ac._classify(q)["has_jira_prefix"] is True


def test_classify_doc_keyword() -> None:
    assert ac._classify("tests for payout")["has_doc_keyword"] is True
    assert ac._classify("readme migration")["has_doc_keyword"] is True
    assert ac._classify("implementation details")["has_doc_keyword"] is False


def test_classify_length_buckets() -> None:
    assert ac._classify("a b").get("length_bucket") == "short"
    assert ac._classify("a b c d e").get("length_bucket") == "medium"
    assert ac._classify("one two three four five six seven eight nine ten").get("length_bucket") == "long"


def test_overlap_at_handles_str_and_int_keys() -> None:
    assert ac._overlap_at({"overlap_at_k": {"10": 0.5}}, 10) == 0.5
    assert ac._overlap_at({"overlap_at_k": {10: 0.5}}, 10) == 0.5
    assert ac._overlap_at({"overlap_at_k": {}}, 10) is None


def _make_entry(query: str, overlap_10: float, top1: bool = True) -> dict:
    return {
        "query": query,
        "overlap_at_k": {"10": overlap_10, "5": overlap_10, "3": overlap_10, "1": overlap_10},
        "top1_changed": top1,
        "base_top10_keys": [f"r::f{i}" for i in range(10)],
        "v8_top10_keys": [f"r::g{i}" for i in range(10)],
    }


def test_top_diff_pairs_sorted_by_churn_descending() -> None:
    per_query = [
        _make_entry("stable", 0.9),
        _make_entry("chaotic", 0.1),
        _make_entry("middle", 0.5),
    ]
    pairs = ac._top_diff_pairs(per_query, top_k=10, limit=3)
    queries_order = [p["query"] for p in pairs]
    assert queries_order == ["chaotic", "middle", "stable"]


def test_slice_stats_handles_empty_subset() -> None:
    s = ac._slice_stats([], lambda _: True, "empty", 10)
    assert s["n"] == 0


def test_aggregate_slices_contains_overall_and_length() -> None:
    per_query = [
        _make_entry("a b", 0.1, top1=True),
        _make_entry("c d e f g", 0.5, top1=False),
        _make_entry("one two three four five six seven eight nine ten", 0.9, top1=True),
    ]
    slices = ac._aggregate_slices(per_query, top_k=10)
    labels = {s["label"] for s in slices}
    assert "overall" in labels
    assert "length=short" in labels
    assert "length=medium" in labels
    assert "length=long" in labels
    assert "uppercase_id=True" in labels and "uppercase_id=False" in labels


def test_fmt_slice_table_includes_n_column() -> None:
    slices = [
        {"label": "overall", "n": 5, "mean_overlap_at_10": 0.4, "median_overlap_at_10": 0.5, "pct_top1_changed": 80.0}
    ]
    table = ac._fmt_slice_table(slices, 10)
    assert "mean_overlap@10" in table
    assert "overall" in table
    assert "0.4" in table


def test_main_end_to_end(tmp_path: Path) -> None:
    """Smoke test: feed a synthetic churn JSON, verify report + diff-pairs written."""
    sample = {
        "config": {"n_queries": 2, "base_model": "base", "v8_model": "v8"},
        "summary": {"mean_overlap_at_k": {"10": 0.25}, "pct_top1_changed": 50.0},
        "per_query": [
            _make_entry("a b", 0.1, top1=True),
            _make_entry("c d", 0.4, top1=False),
        ],
    }
    input_path = tmp_path / "churn.json"
    diff_path = tmp_path / "diff.jsonl"
    report_path = tmp_path / "report.md"
    input_path.write_text(json.dumps(sample))

    old_argv = sys.argv
    sys.argv = [
        "analyze_churn",
        "--input",
        str(input_path),
        "--diff-pairs",
        str(diff_path),
        "--report",
        str(report_path),
    ]
    try:
        rc = ac.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    assert report_path.exists()
    assert diff_path.exists()
    body = report_path.read_text()
    assert "# Churn Replay Analysis" in body
    assert "overall" in body
    # diff pairs — the more-churn query ("a b") comes first
    lines = [json.loads(line) for line in diff_path.read_text().strip().split("\n")]
    assert lines[0]["query"] == "a b"
