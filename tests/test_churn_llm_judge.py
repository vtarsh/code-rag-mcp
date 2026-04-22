"""Unit tests for scripts/churn_llm_judge — dry-run path + helpers only.

Live API path is exercised manually with --run; not in CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import churn_llm_judge as clj  # type: ignore[import-not-found]


def _pair(query: str, base_keys=None, v8_keys=None) -> dict:
    return {
        "query": query,
        "base_top10_keys": base_keys or ["repo::a.ts", "repo::b.ts"],
        "v8_top10_keys": v8_keys or ["repo::c.ts", "repo::d.ts"],
        "overlap_at_10": 0.1,
    }


def test_format_keys_numbers_entries() -> None:
    out = clj._format_keys(["r::x.ts", "r::y.ts"], "LIST A")
    assert "LIST A:" in out
    assert "1. r::x.ts" in out
    assert "2. r::y.ts" in out


def test_format_keys_caps_at_10() -> None:
    out = clj._format_keys([f"r::f{i}.ts" for i in range(25)], "LIST A")
    assert "10. r::f9.ts" in out
    assert "11. r::f10.ts" not in out


def test_build_prompt_includes_query_and_both_lists() -> None:
    pair = _pair("payout method unimplemented", ["r::payout.js"], ["r::methods/index.js"])
    system, user = clj._build_prompt(pair)
    assert "relevance judge" in system
    assert "payout method unimplemented" in user
    assert "LIST A (base)" in user and "r::payout.js" in user
    assert "LIST B (v8)" in user and "r::methods/index.js" in user
    assert "'a'" in user and "'b'" in user and "'tie'" in user


def test_build_prompt_falls_back_to_raw_top10_when_keys_missing() -> None:
    pair = {
        "query": "test",
        "base_top10": [{"repo_name": "r", "file_path": "x.ts"}],
        "v8_top10": [{"repo_name": "r", "file_path": "y.ts"}],
    }
    _, user = clj._build_prompt(pair)
    assert "r::x.ts" in user
    assert "r::y.ts" in user


def test_aggregate_counts_verdicts() -> None:
    results = [
        {"judge": {"verdict": "a", "confidence": 0.8, "usage": {"input_tokens": 100, "output_tokens": 20}}},
        {"judge": {"verdict": "b", "confidence": 0.9, "usage": {"input_tokens": 100, "output_tokens": 20}}},
        {"judge": {"verdict": "b", "confidence": 0.7, "usage": {"input_tokens": 100, "output_tokens": 20}}},
        {"judge": {"verdict": "tie", "confidence": 0.6, "usage": {"input_tokens": 100, "output_tokens": 20}}},
    ]
    summary = clj._aggregate(results)
    assert summary["n"] == 4
    assert summary["base_wins"] == 1
    assert summary["v8_wins"] == 2
    assert summary["ties"] == 1
    assert summary["base_win_rate"] == 0.25
    assert summary["v8_win_rate"] == 0.5
    assert summary["tie_rate"] == 0.25
    assert summary["mean_confidence"]["v8"] == 0.8  # (0.9 + 0.7) / 2
    assert summary["usage_total"] == {"input_tokens": 400, "output_tokens": 80}


def test_aggregate_handles_empty_results() -> None:
    assert clj._aggregate([]) == {"n": 0}


def test_aggregate_handles_all_one_verdict() -> None:
    results = [
        {"judge": {"verdict": "b", "confidence": 0.9, "usage": {"input_tokens": 100, "output_tokens": 20}}},
        {"judge": {"verdict": "b", "confidence": 0.8, "usage": {"input_tokens": 100, "output_tokens": 20}}},
    ]
    summary = clj._aggregate(results)
    assert summary["v8_wins"] == 2
    assert summary["base_wins"] == 0
    assert summary["mean_confidence"]["base"] is None
    assert summary["mean_confidence"]["v8"] == 0.85


def test_dry_run_main_does_not_import_anthropic(tmp_path: Path, capsys) -> None:
    """Dry-run mode must not touch the anthropic SDK."""
    diff_pairs = tmp_path / "diff_pairs.jsonl"
    with diff_pairs.open("w") as f:
        f.write(json.dumps(_pair("test query")) + "\n")

    old_argv = sys.argv
    sys.argv = ["churn_llm_judge", "--input", str(diff_pairs)]
    try:
        rc = clj.main()
    finally:
        sys.argv = old_argv

    assert rc == 0
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "test query" in captured.out
    assert "ANTHROPIC_API_KEY" in captured.out


def test_run_without_api_key_errors(tmp_path: Path, capsys, monkeypatch) -> None:
    """--run without ANTHROPIC_API_KEY must exit with error code."""
    diff_pairs = tmp_path / "diff_pairs.jsonl"
    with diff_pairs.open("w") as f:
        f.write(json.dumps(_pair("test")) + "\n")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    old_argv = sys.argv
    sys.argv = ["churn_llm_judge", "--input", str(diff_pairs), "--run"]
    try:
        rc = clj.main()
    finally:
        sys.argv = old_argv

    assert rc == 1
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err


def test_missing_input_errors(tmp_path: Path) -> None:
    old_argv = sys.argv
    sys.argv = ["churn_llm_judge", "--input", str(tmp_path / "does_not_exist.jsonl")]
    try:
        rc = clj.main()
    finally:
        sys.argv = old_argv
    assert rc == 1


def test_max_pairs_limit_respected(tmp_path: Path, capsys) -> None:
    diff_pairs = tmp_path / "diff_pairs.jsonl"
    with diff_pairs.open("w") as f:
        for i in range(10):
            f.write(json.dumps(_pair(f"q{i}")) + "\n")

    old_argv = sys.argv
    sys.argv = ["churn_llm_judge", "--input", str(diff_pairs), "--max-pairs", "3"]
    try:
        rc = clj.main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    captured = capsys.readouterr()
    assert "loaded 3 diff pairs" in captured.out
