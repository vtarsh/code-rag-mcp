#!/usr/bin/env python3
"""P1b: Top-K churn replay on real MCP queries.

Compares two rerankers (base + v8 by default) on the same production hybrid
retrieval pool across a fixed set of sampled real queries. Measures how much
the v8 FT reshuffles top-K vs. the untuned base model — a runtime-transfer
signal independent of Jira ground truth.

Why:
  Jira eval (gte_v8_hybrid.json) shows v8 r@10 = 0.6621 (+0.034 vs baseline).
  But Jira queries are enriched tickets, not the short identifier-heavy queries
  users actually type. Churn answers: does v8 actually reorder top-10 on real
  queries, or is it a no-op outside the Jira distribution?

Metrics per query:
  - overlap@K   (|A ∩ B| / K) for K ∈ {1, 3, 5, 10}
  - jaccard@K   (|A ∩ B| / |A ∪ B|)
  - top1_changed (bool)
  - rank_diff_mean for shared items (sum |rank_A - rank_B| / |A ∩ B|)

Aggregates:
  - mean/median overlap & jaccard per K
  - % queries with top-1 change
  - % queries with >50% churn at K=10

Output JSON schema:
  {
    "config": {...},
    "per_query": [
      {"query": "...", "base_top10": [...], "v8_top10": [...], metrics...},
      ...
    ],
    "summary": {
      "n_queries": int,
      "mean_overlap_at_k": {"1": ..., "3": ..., "5": ..., "10": ...},
      "median_overlap_at_k": {...},
      "mean_jaccard_at_10": float,
      "pct_top1_changed": float,
      "pct_high_churn_at_10": float,
    }
  }

Usage:
  python3.12 scripts/churn_replay.py \
    --queries profiles/pay-com/real_queries/sampled.jsonl \
    --base-model Alibaba-NLP/gte-reranker-modernbert-base \
    --v8-model profiles/pay-com/models/reranker_ft_gte_v8 \
    --output profiles/pay-com/churn_replay/v8_vs_base.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._common import setup_paths

REPO_ROOT = setup_paths()


def _load_queries(path: Path, limit: int | None) -> list[str]:
    out: list[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            q = (rec.get("query") or "").strip()
            if q:
                out.append(q)
            if limit is not None and len(out) >= limit:
                break
    return out


def _key(r: dict) -> str:
    """Churn key — (repo, file_path). Chunk-level churn would be noisier."""
    return f"{r.get('repo_name', '')}::{r.get('file_path', '')}"


def _top_keys(results: list[dict], k: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in results:
        key = _key(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= k:
            break
    return out


def _overlap_at_k(a: list[str], b: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    a_set = set(a[:k])
    b_set = set(b[:k])
    return len(a_set & b_set) / k


def _jaccard_at_k(a: list[str], b: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    a_set = set(a[:k])
    b_set = set(b[:k])
    union = a_set | b_set
    return len(a_set & b_set) / len(union) if union else 0.0


def _rank_diff_mean(a: list[str], b: list[str], k: int) -> float | None:
    a_rank = {key: i + 1 for i, key in enumerate(a[:k])}
    b_rank = {key: i + 1 for i, key in enumerate(b[:k])}
    shared = set(a_rank) & set(b_rank)
    if not shared:
        return None
    diffs = [abs(a_rank[s] - b_rank[s]) for s in shared]
    return sum(diffs) / len(diffs)


class _CrossEncoderAdapter:
    """Mirror of scripts/eval_finetune.py::_CrossEncoderAdapter.

    hybrid.rerank() expects `rerank(query, documents, limit)` → list[float].
    CrossEncoder.predict(pairs) returns a numpy array; this wraps it.
    """

    def __init__(self, model, *, batch_size: int = 2):
        self._model = model
        self._batch_size = batch_size

    @property
    def provider_name(self) -> str:
        return "churn_replay_adapter"

    def rerank(self, query: str, documents: list[str], limit: int = 10) -> list[float]:
        pairs = [(query, doc) for doc in documents]
        scores = self._model.predict(pairs, batch_size=self._batch_size)
        return [float(s) for s in scores]


def _run_one_reranker(
    queries: list[str],
    model_path: str,
    *,
    batch_size: int,
    max_length: int,
    top_k: int,
    label: str,
) -> list[list[dict]]:
    """Load model, run hybrid_search for each query, return list of top-K results."""
    from sentence_transformers import CrossEncoder

    from src.search.hybrid import hybrid_search

    print(f"[{label}] loading model: {model_path}", flush=True)
    t0 = time.perf_counter()
    model = CrossEncoder(model_path, trust_remote_code=True, max_length=max_length)
    adapter = _CrossEncoderAdapter(model, batch_size=batch_size)
    print(f"[{label}] loaded in {time.perf_counter() - t0:.1f}s", flush=True)

    out: list[list[dict]] = []
    for i, q in enumerate(queries):
        t = time.perf_counter()
        try:
            ranked = hybrid_search(q, limit=top_k, reranker_override=adapter)[0]
        except Exception as e:  # pragma: no cover
            print(f"[{label}] query #{i} ERROR: {e}", flush=True)
            ranked = []
        lat = time.perf_counter() - t
        minimal = [
            {
                "repo_name": r.get("repo_name", ""),
                "file_path": r.get("file_path", ""),
                "file_type": r.get("file_type", ""),
                "chunk_type": r.get("chunk_type", ""),
                "combined_score": r.get("combined_score"),
                "rerank_score": r.get("rerank_score"),
            }
            for r in (ranked or [])[:top_k]
        ]
        out.append(minimal)
        if (i + 1) % 20 == 0 or i + 1 == len(queries):
            print(f"[{label}] {i + 1}/{len(queries)} lat={lat:.2f}s q={q[:60]!r}", flush=True)
    return out


def _compute_metrics(base_results: list[list[dict]], v8_results: list[list[dict]], top_k: int) -> list[dict]:
    per_query: list[dict] = []
    for a_raw, b_raw in zip(base_results, v8_results, strict=False):
        a = _top_keys(a_raw, top_k)
        b = _top_keys(b_raw, top_k)
        metrics: dict[str, Any] = {
            "base_top10_keys": a,
            "v8_top10_keys": b,
            "top1_changed": bool(a[:1] != b[:1]),
            "overlap_at_k": {k: _overlap_at_k(a, b, k) for k in (1, 3, 5, 10) if k <= top_k},
            "jaccard_at_k": {k: _jaccard_at_k(a, b, k) for k in (3, 5, 10) if k <= top_k},
            "rank_diff_mean_at_10": _rank_diff_mean(a, b, top_k),
        }
        per_query.append(metrics)
    return per_query


def _aggregate(per_query: list[dict], top_k: int) -> dict:
    n = len(per_query)

    def _collect(path: tuple[str, int]) -> list[float]:
        section, k = path
        return [m[section][k] for m in per_query if m[section].get(k) is not None]

    mean_overlap = {k: round(statistics.fmean(_collect(("overlap_at_k", k))), 4) for k in (1, 3, 5, 10) if k <= top_k}
    median_overlap = {
        k: round(statistics.median(_collect(("overlap_at_k", k))), 4) for k in (1, 3, 5, 10) if k <= top_k
    }
    mean_jaccard = {k: round(statistics.fmean(_collect(("jaccard_at_k", k))), 4) for k in (3, 5, 10) if k <= top_k}

    pct_top1 = round(100.0 * sum(1 for m in per_query if m["top1_changed"]) / n, 2) if n else 0.0
    high_churn = sum(1 for m in per_query if m["overlap_at_k"].get(top_k, 1) < 0.5)
    pct_high_churn = round(100.0 * high_churn / n, 2) if n else 0.0

    rank_diffs = [m["rank_diff_mean_at_10"] for m in per_query if m["rank_diff_mean_at_10"] is not None]
    mean_rank_diff = round(statistics.fmean(rank_diffs), 3) if rank_diffs else None

    return {
        "n_queries": n,
        "mean_overlap_at_k": mean_overlap,
        "median_overlap_at_k": median_overlap,
        "mean_jaccard_at_k": mean_jaccard,
        "pct_top1_changed": pct_top1,
        "pct_high_churn_at_10": pct_high_churn,
        "mean_rank_diff_at_10": mean_rank_diff,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--queries", type=Path, default=Path("profiles/pay-com/real_queries/sampled.jsonl"))
    p.add_argument("--base-model", default="Alibaba-NLP/gte-reranker-modernbert-base")
    p.add_argument("--v8-model", default="profiles/pay-com/models/reranker_ft_gte_v8")
    p.add_argument("--output", type=Path, default=Path("profiles/pay-com/churn_replay/v8_vs_base.json"))
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--batch-size", type=int, default=2, help="rerank batch_size (pitfall: 1 is safest on MPS)")
    p.add_argument("--max-length", type=int, default=256)
    p.add_argument("--limit", type=int, default=None, help="process only first N queries (debug)")
    args = p.parse_args()

    if not args.queries.exists():
        print(f"ERROR: queries file not found: {args.queries}", file=sys.stderr)
        return 1

    queries = _load_queries(args.queries, args.limit)
    if not queries:
        print("ERROR: no queries loaded", file=sys.stderr)
        return 1
    print(f"loaded {len(queries)} queries", flush=True)

    t_start = time.perf_counter()

    base_results = _run_one_reranker(
        queries,
        args.base_model,
        batch_size=args.batch_size,
        max_length=args.max_length,
        top_k=args.top_k,
        label="base",
    )
    t_base = time.perf_counter() - t_start

    v8_results = _run_one_reranker(
        queries,
        args.v8_model,
        batch_size=args.batch_size,
        max_length=args.max_length,
        top_k=args.top_k,
        label="v8",
    )
    t_v8 = time.perf_counter() - t_start - t_base

    per_query_metrics = _compute_metrics(base_results, v8_results, args.top_k)
    summary = _aggregate(per_query_metrics, args.top_k)

    payload = {
        "config": {
            "queries_path": str(args.queries),
            "base_model": args.base_model,
            "v8_model": args.v8_model,
            "top_k": args.top_k,
            "batch_size": args.batch_size,
            "max_length": args.max_length,
            "n_queries": len(queries),
            "t_base_seconds": round(t_base, 1),
            "t_v8_seconds": round(t_v8, 1),
        },
        "per_query": [
            {"query": q, **m, "base_top10": base_results[i], "v8_top10": v8_results[i]}
            for i, (q, m) in enumerate(zip(queries, per_query_metrics, strict=False))
        ],
        "summary": summary,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n=== SUMMARY ===", flush=True)
    for k, v in summary.items():
        print(f"  {k}: {v}", flush=True)
    print(f"\noutput: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
