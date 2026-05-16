#!/usr/bin/env python3
"""Evaluate hit@k on jira_eval_clean.jsonl (n=663).

Runs the production hybrid_search pipeline against each query and scores
against expected_paths.  Outputs JSON with per-query results + aggregates
suitable for bootstrap_eval_ci.py.

Usage:
    python3 scripts/eval/eval_jira_clean.py --out=bench_runs/run_repo_prefilter.json
    python3 scripts/eval/eval_jira_clean.py --limit=5 --out=bench_runs/run_h5.json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts._common import setup_paths

setup_paths()

from src.search.hybrid import hybrid_search

EVAL_PATH = REPO_ROOT / "profiles" / "pay-com" / "eval" / "jira_eval_clean.jsonl"


def _load_eval(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _expected_set(row: dict) -> set[tuple[str, str]]:
    """{(repo, file_path), ...} from expected_paths."""
    return {(ep["repo_name"], ep["file_path"]) for ep in row.get("expected_paths", [])}


def _hit_at_k(expected: set[tuple[str, str]], retrieved: list[tuple[str, str]], k: int) -> int:
    if not expected:
        return 0
    top_k = set(retrieved[:k])
    return 1 if (expected & top_k) else 0


def _recall_at_k(expected: set[tuple[str, str]], retrieved: list[tuple[str, str]], k: int) -> float:
    if not expected:
        return 0.0
    top_k = set(retrieved[:k])
    return len(expected & top_k) / len(expected)


def _ndcg_at_k(expected: set[tuple[str, str]], retrieved: list[tuple[str, str]], k: int) -> float:
    if not expected:
        return 0.0
    dcg = 0.0
    for i, key in enumerate(retrieved[:k], start=1):
        if key in expected:
            dcg += 1.0 / math.log2(i + 1)
    ideal_n = min(len(expected), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_n + 1))
    return dcg / idcg if idcg > 0 else 0.0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--eval", type=Path, default=EVAL_PATH, help="Path to jira_eval_clean.jsonl")
    p.add_argument("--out", type=Path, required=True, help="Output JSON path")
    p.add_argument("--limit", type=int, default=10, help="K for hit@k / recall@k / nDCG@k")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    rows = _load_eval(args.eval)
    eval_per_query: list[dict] = []
    sum_hit = 0.0
    sum_recall = 0.0
    sum_ndcg = 0.0

    for idx, row in enumerate(rows, 1):
        query = row["query"]
        expected = _expected_set(row)
        ranked, err, _total = hybrid_search(query, limit=args.limit)
        retrieved = [(r["repo_name"], r["file_path"]) for r in ranked]

        hit = _hit_at_k(expected, retrieved, args.limit)
        recall = _recall_at_k(expected, retrieved, args.limit)
        ndcg = _ndcg_at_k(expected, retrieved, args.limit)

        sum_hit += hit
        sum_recall += recall
        sum_ndcg += ndcg

        q_result = {
            "query": query,
            "query_id": row.get("id", ""),
            "hit_at_k": hit,
            "recall_at_k": round(recall, 4),
            "ndcg_at_k": round(ndcg, 4),
            "strata": row.get("strata", []),
        }
        eval_per_query.append(q_result)

        if args.verbose:
            status = "✓" if hit else "✗"
            print(f"[{idx}/{len(rows)}] {status} {query[:60]}... hit={hit} recall={recall:.3f}")

    n = len(rows)
    result = {
        "config": {"eval": str(args.eval), "limit": args.limit},
        "aggregates": {
            "n": n,
            f"hit_at_{args.limit}": round(sum_hit / n, 4) if n else None,
            f"recall_at_{args.limit}": round(sum_recall / n, 4) if n else None,
            f"ndcg_at_{args.limit}": round(sum_ndcg / n, 4) if n else None,
        },
        "eval_per_query": eval_per_query,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2))
    print(f"Wrote {args.out}")
    print(
        f"hit@{args.limit}={result['aggregates'][f'hit_at_{args.limit}']:.2%}  "
        f"recall@{args.limit}={result['aggregates'][f'recall_at_{args.limit}']:.2%}  "
        f"nDCG@{args.limit}={result['aggregates'][f'ndcg_at_{args.limit}']:.2%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
