#!/usr/bin/env python3
"""Rank-sensitive eval for autoresearch loop.

Why not benchmark_queries.py? That script measures SET membership in a
top-20 result set — scalar knobs (RRF_K, KEYWORD_WEIGHT, GOTCHAS_BOOST,
REFERENCE_BOOST) only reorder results, they almost never push repos in
or out of top-20. The resulting metric is a step function that sits flat
against perturbations.

This script runs the same conceptual_queries from benchmarks.yaml but:
  1. Uses a smaller top-K (default 3) so tuning affects inclusion.
  2. Reports MRR-style rank score: 1.0 for rank-1, 0.5 for rank-2,
     0.333 for rank-3, 0 for not-in-top-K.
  3. Averages across (query, expected_repo) pairs, weighted by the
     expected_repo weight from benchmarks.yaml.

Emits a single final line the autoresearch loop parses:
  Average MRR score: 0.xxx
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.bench.bench_utils import resolve_profile_dir, run_hybrid_search

TOP_K = int(os.environ.get("AUTORESEARCH_TOP_K", "3"))


def load_queries() -> list[dict]:
    prof = resolve_profile_dir()
    bench = prof / "benchmarks.yaml"
    data = yaml.safe_load(bench.read_text())
    return data.get("conceptual_queries", [])


def rank_of(repo: str, ranked_list: list) -> int | None:
    for i, item in enumerate(ranked_list, start=1):
        if item[0] == repo:
            return i
    return None


def run_query(query_entry: dict) -> tuple[float, list[str]]:
    """Return (weighted_rank_score, missed_repos) for one query entry."""
    expected = query_entry.get("expected_repos", {})
    if not expected:
        return 1.0, []
    sub_queries = query_entry.get("search_queries") or [query_entry.get("question", "")]
    sub_queries = [q for q in sub_queries if q]

    # Union ranks across sub_queries — best rank wins per repo.
    best_rank: dict[str, int] = {}
    for sq in sub_queries:
        result = run_hybrid_search(sq, limit=TOP_K)
        results = result.get("results") or []
        for repo in expected:
            r = rank_of(repo, results)
            if r is not None:
                prev = best_rank.get(repo)
                if prev is None or r < prev:
                    best_rank[repo] = r

    total_weight = sum(expected.values())
    weighted_score = 0.0
    missed = []
    for repo, weight in expected.items():
        rank = best_rank.get(repo)
        if rank is None:
            missed.append(repo)
            continue
        # reciprocal-rank style
        weighted_score += weight * (1.0 / rank)
    return (weighted_score / total_weight if total_weight else 0.0), missed


def main() -> int:
    t0 = time.time()
    queries = load_queries()
    scores = []
    for qe in queries:
        qid = qe.get("id", "?")
        try:
            s, missed = run_query(qe)
        except Exception as e:
            print(f"  [{qid}] ERROR: {e}")
            continue
        scores.append(s)
        status = "PASS" if s >= 0.5 else "PARTIAL" if s >= 0.2 else "FAIL"
        miss_str = f" missed={','.join(missed)}" if missed else ""
        print(f"  [{qid}] {status} rank_score={s:.4f}{miss_str}")
    avg = sum(scores) / len(scores) if scores else 0.0
    dur = time.time() - t0
    print()
    print(f"Average MRR score: {avg:.4f}")
    print(f"Top-K: {TOP_K}")
    print(f"Eval time: {dur:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
