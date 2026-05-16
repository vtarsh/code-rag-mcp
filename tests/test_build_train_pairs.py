"""Tests for scripts/build_train_pairs_v2.py.

Covers:
- load_prod_queries dedupes and filters (doc-intent only)
- query_disjoint excludes eval queries
- path_disjoint excludes eval expected_paths
- pair row schema matches contract {q, pos, hard_negs, query_freq}
- --probe=N limits processed queries to N
- pick_positives_and_hard_negatives slicing + eval-path drop

All tests mock the CrossEncoder + FTS5 — no models loaded, no daemon.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "build_train_pairs_v2", REPO_ROOT / "scripts" / "build" / "build_train_pairs_v2.py"
)
assert _SPEC and _SPEC.loader
btp = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(btp)


# ----- helpers ---------------------------------------------------------------


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _tool_call(query: str, tool: str = "search") -> dict:
    return {"ts": "x", "tool": tool, "args": {"query": query}, "duration_ms": 0}


def _eval_row(query: str, paths: list[tuple[str, str]]) -> dict:
    return {
        "query": query,
        "expected_paths": [{"repo_name": r, "file_path": p} for r, p in paths],
    }


def _fake_cands(paths: list[tuple[str, str]]) -> list[dict]:
    return [{"repo_name": r, "file_path": p, "file_type": "docs", "snippet": f"{r}/{p}"} for r, p in paths]


# ----- load_prod_queries -----------------------------------------------------


def test_load_prod_queries_dedupes(tmp_path):
    """Duplicate queries collapse to a single row with summed freq."""
    log = tmp_path / "tool_calls.jsonl"
    _write_jsonl(
        log,
        [
            _tool_call("trustly verification webhook"),
            _tool_call("trustly verification webhook"),  # dup
            _tool_call("trustly verification webhook"),  # dup
            _tool_call("nuvei refund flow"),
        ],
    )
    qs = btp.load_prod_queries(log, btp._query_wants_docs)
    qmap = dict(qs)
    assert qmap["trustly verification webhook"] == 3
    assert qmap["nuvei refund flow"] == 1


def test_load_prod_queries_filters_doc_intent(tmp_path):
    """Code-signature queries filtered out by _query_wants_docs."""
    log = tmp_path / "tool_calls.jsonl"
    _write_jsonl(
        log,
        [
            _tool_call("trustly verification webhook"),  # doc-intent (multi-token, no sig)
            _tool_call("foo_bar()"),  # code sig — drop
            _tool_call("grpc-apm-payper service"),  # repo token — drop
            _tool_call(""),  # empty — drop
        ],
    )
    qs = btp.load_prod_queries(log, btp._query_wants_docs)
    queries_only = [q for q, _ in qs]
    assert "trustly verification webhook" in queries_only
    assert all("foo_bar(" not in q for q in queries_only)
    assert all("grpc-apm-payper" not in q for q in queries_only)


def test_load_prod_queries_only_search_and_analyze_task(tmp_path):
    """Other tools (health_check, broken) ignored."""
    log = tmp_path / "tool_calls.jsonl"
    _write_jsonl(
        log,
        [
            _tool_call("docs guide", tool="health_check"),  # wrong tool — drop
            _tool_call("docs guide", tool="search"),  # keep
            {
                "ts": "x",
                "tool": "analyze_task",
                "args": {"description": "trustly refund webhook docs"},
                "duration_ms": 0,
            },
        ],
    )
    qs = btp.load_prod_queries(log, btp._query_wants_docs)
    queries_only = {q for q, _ in qs}
    assert "docs guide" in queries_only
    assert "trustly refund webhook docs" in queries_only
    assert len(queries_only) == 2


# ----- query_disjoint / path_disjoint ---------------------------------------


def test_query_disjoint_excludes_eval_queries(tmp_path):
    """Queries appearing in eval set are dropped (case-insensitive)."""
    eval_path = tmp_path / "eval.jsonl"
    _write_jsonl(eval_path, [_eval_row("Trustly Refund Webhook", [("r", "p")])])
    qs, _ps = btp.load_eval_disjoint_set((eval_path,))
    assert "trustly refund webhook" in qs
    # query_disjoint returns False → drop
    assert btp.query_disjoint("trustly refund webhook", qs) is False
    assert btp.query_disjoint("TRUSTLY REFUND WEBHOOK", qs) is False
    assert btp.query_disjoint("nuvei payout", qs) is True


def test_path_disjoint_excludes_eval_paths(tmp_path):
    """Paths in expected_paths are excluded; novel paths kept."""
    eval_path = tmp_path / "eval.jsonl"
    _write_jsonl(
        eval_path,
        [
            _eval_row("q1", [("nuvei-docs", "docs/payout.md")]),
            _eval_row("q2", [("vyne-docs", "docs/checkout.md")]),
        ],
    )
    _, ps = btp.load_eval_disjoint_set((eval_path,))
    assert btp.path_disjoint("nuvei-docs", "docs/payout.md", ps) is False
    assert btp.path_disjoint("vyne-docs", "docs/checkout.md", ps) is False
    assert btp.path_disjoint("nuvei-docs", "docs/other.md", ps) is True


# ----- pair row schema -------------------------------------------------------


def test_pair_row_schema():
    """Output row has fields {q, pos, hard_negs, query_freq}."""
    pos = [{"repo_name": "r1", "file_path": "p1", "_score": 0.9}]
    hn = [{"repo_name": "r2", "file_path": "p2", "_score": 0.4}]
    row = btp.build_pair_row("test query", 5, pos, hn)
    assert set(row.keys()) == {"q", "pos", "hard_negs", "query_freq"}
    assert row["q"] == "test query"
    assert row["query_freq"] == 5
    assert row["pos"] == [{"repo_name": "r1", "file_path": "p1"}]
    assert row["hard_negs"] == [{"repo_name": "r2", "file_path": "p2"}]


# ----- pick_positives_and_hard_negatives -----------------------------------


def test_pick_drops_eval_paths():
    """Items with paths in eval_paths are filtered from both pos and hn."""
    ranked = _fake_cands([(f"r{i}", f"p{i}") for i in range(1, 31)])
    eval_paths = {("r2", "p2"), ("r12", "p12")}
    pos, hn = btp.pick_positives_and_hard_negatives(
        ranked,
        (1, 3),
        (11, 30),
        eval_paths,
    )
    assert ("r2", "p2") not in {(p["repo_name"], p["file_path"]) for p in pos}
    assert ("r12", "p12") not in {(n["repo_name"], n["file_path"]) for n in hn}
    # Positives 1,3 (rank 2 dropped) → 2 left
    assert len(pos) == 2
    # Hard negatives 11..30 minus rank 12 → 19 left
    assert len(hn) == 19


def test_parse_rank_range():
    assert btp._parse_rank_range("1-3") == (1, 3)
    assert btp._parse_rank_range("11-30") == (11, 30)
    with pytest.raises(ValueError):
        btp._parse_rank_range("0-5")
    with pytest.raises(ValueError):
        btp._parse_rank_range("5-3")


# ----- main() integration with mocks ----------------------------------------


def _patched_main(argv: list[str], cands_per_query: list[tuple[str, str]]):
    """Run main() with retrieve_candidates + CrossEncoder mocked.

    Returns the dictrows written to the output JSONL.
    """
    fake_cands = _fake_cands(cands_per_query)
    fake_reranker = MagicMock()
    # Score: descending by index → preserves input order as rank.
    fake_reranker.predict.return_value = [1.0 - 0.001 * i for i in range(len(fake_cands))]

    with (
        patch.object(btp, "retrieve_candidates", return_value=fake_cands),
        patch("sentence_transformers.CrossEncoder", return_value=fake_reranker),
    ):
        rc = btp.main(argv)
    return rc


def test_probe_mode_limits_to_10(tmp_path):
    """--probe=10 limits processing to 10 queries."""
    log = tmp_path / "tool_calls.jsonl"
    _write_jsonl(log, [_tool_call(f"docs guide {i}") for i in range(50)])
    out = tmp_path / "train.jsonl"

    cands = [(f"r{i}", f"p{i}") for i in range(1, 31)]  # 30 candidates per query
    rc = _patched_main(
        [
            "--queries",
            str(log),
            "--out",
            str(out),
            "--probe",
            "10",
            "--seed",
            "42",
        ],
        cands,
    )
    assert rc == 0

    written = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(written) == 10
    for row in written:
        assert set(row.keys()) == {"q", "pos", "hard_negs", "query_freq"}
        assert len(row["pos"]) == 3
        assert len(row["hard_negs"]) == 20


def test_main_skips_query_if_no_positives(tmp_path):
    """Queries whose positives ALL collide with eval paths are skipped."""
    log = tmp_path / "tool_calls.jsonl"
    _write_jsonl(log, [_tool_call("docs guide one"), _tool_call("docs guide two")])
    eval_path = tmp_path / "eval.jsonl"
    # All 3 positive paths collide with eval → no positives → row skipped.
    _write_jsonl(
        eval_path,
        [
            _eval_row("unrelated_q", [("r1", "p1"), ("r2", "p2"), ("r3", "p3")]),
        ],
    )
    out = tmp_path / "train.jsonl"
    cands = [(f"r{i}", f"p{i}") for i in range(1, 31)]
    rc = _patched_main(
        [
            "--queries",
            str(log),
            "--out",
            str(out),
            "--eval-disjoint",
            str(eval_path),
            "--seed",
            "42",
        ],
        cands,
    )
    assert rc == 0
    written = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert written == []  # both queries skipped (no positives left)
