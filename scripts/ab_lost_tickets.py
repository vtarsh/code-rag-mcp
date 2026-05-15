#!/usr/bin/env python3
"""A/B investigation: re-run hybrid search on tickets lost in P0a eval.

Reads ticket_ids where the fts_only+fallback snapshot hit GT but the hybrid
snapshot missed (r@10=0 vs r@10>0 baseline). Runs hybrid_search() again for
each one with the currently active env gates (CODE_RAG_DISABLE_PENALTIES and
CODE_RAG_DISABLE_CODE_FACTS) so we can isolate the cause of the regression.

Usage:
  # Control — reproduce the original lost-ticket r@10
  python3.12 scripts/ab_lost_tickets.py

  # Variant A — penalties disabled
  CODE_RAG_DISABLE_PENALTIES=1 python3.12 scripts/ab_lost_tickets.py

  # Variant B — code_facts/env_vars disabled
  CODE_RAG_DISABLE_CODE_FACTS=1 python3.12 scripts/ab_lost_tickets.py

Output: mean r@10 over the lost subset + per-ticket deltas for the top
regressors. Uses the baseline reranker (same base_model the snapshot used).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HYBRID_SNAP = Path("profiles/pay-com/finetune_history/gte_v8_hybrid.json")
FTS_SNAP = Path("profiles/pay-com/finetune_history/gte_v8_fallback.json")
BASE_MODEL = "Alibaba-NLP/gte-reranker-modernbert-base"


def load_lost_tickets() -> list[str]:
    hb = json.loads(HYBRID_SNAP.read_text())["per_task_baseline"]
    fb = json.loads(FTS_SNAP.read_text())["per_task_baseline"]
    return [tid for tid in hb if hb[tid].get("recall_at_10", 0) == 0 and fb.get(tid, {}).get("recall_at_10", 0) > 0]


def load_task_for_ticket(conn: sqlite3.Connection, ticket: str) -> dict | None:
    row = conn.execute(
        "SELECT ticket_id, summary, description, jira_comments, repos_changed FROM task_history WHERE ticket_id = ?",
        (ticket,),
    ).fetchone()
    if not row:
        return None
    try:
        repos = json.loads(row["repos_changed"])
    except Exception:
        return None
    if not repos:
        return None
    comments = []
    if row["jira_comments"]:
        try:
            comments = json.loads(row["jira_comments"]) or []
        except Exception:
            comments = []
    return {
        "ticket_id": row["ticket_id"],
        "summary": row["summary"] or "",
        "description": row["description"] or "",
        "jira_comments": comments,
        "expected_repos": list(repos),
    }


def recall_at_k(ranked_repos: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 0.0
    top = set(ranked_repos[:k])
    return len(top & expected) / len(expected)


def dedup_by_repo(ranked: list[dict]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in ranked:
        name = r.get("repo_name", "")
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Cap number of tickets to eval")
    parser.add_argument("--fts-limit", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--out", type=Path, default=None, help="Optional JSON path to dump per-ticket results")
    args = parser.parse_args()

    # A/B env reporting
    flags = {
        "CODE_RAG_DISABLE_PENALTIES": os.getenv("CODE_RAG_DISABLE_PENALTIES", "0"),
        "CODE_RAG_DISABLE_CODE_FACTS": os.getenv("CODE_RAG_DISABLE_CODE_FACTS", "0"),
        "AB_FALLBACK_ENRICH": os.getenv("AB_FALLBACK_ENRICH", "0"),
        "AB_ENRICHED_ALWAYS": os.getenv("AB_ENRICHED_ALWAYS", "0"),
    }
    print(f"A/B flags: {flags}")
    fallback_on = flags["AB_FALLBACK_ENRICH"] == "1"
    enriched_always = flags["AB_ENRICHED_ALWAYS"] == "1"

    # Route fts_limit → RERANK_POOL_SIZE (must be set before hybrid import)
    os.environ["CODE_RAG_RERANK_POOL_SIZE"] = str(args.fts_limit)

    lost = load_lost_tickets()
    if args.limit:
        lost = lost[: args.limit]
    print(f"Lost tickets to re-eval: {len(lost)}")

    from sentence_transformers import CrossEncoder

    from scripts.eval_finetune import _CrossEncoderAdapter
    from scripts.prepare_finetune_data import build_query_text, preclean_for_fts
    from src.search.hybrid import hybrid_search

    t0 = time.perf_counter()
    model = CrossEncoder(BASE_MODEL, trust_remote_code=True, max_length=args.max_length)
    adapter = _CrossEncoderAdapter(model, batch_size=args.batch_size)
    print(f"loaded {BASE_MODEL} in {time.perf_counter() - t0:.1f}s")

    conn = sqlite3.connect("db/tasks.db", timeout=30)
    conn.row_factory = sqlite3.Row

    per_ticket: dict[str, dict] = {}
    recalls: list[float] = []
    latencies: list[float] = []

    for idx, tid in enumerate(lost):
        task = load_task_for_ticket(conn, tid)
        if not task:
            continue
        expected = set(task["expected_repos"])
        if enriched_always:
            query = preclean_for_fts(build_query_text(task, use_description=True)) or task["summary"]
        else:
            query = task["summary"]
        t_q = time.perf_counter()
        try:
            ranked, vec_err, total = hybrid_search(query, limit=args.fts_limit, reranker_override=adapter)
        except Exception as e:
            per_ticket[tid] = {"error": f"{type(e).__name__}: {e}", "recall_at_10": 0.0}
            continue
        lat = time.perf_counter() - t_q

        # Fallback-enrich variant: when pool ≤ 50 retry with enriched query.
        fallback_used = False
        if fallback_on and total <= 50:
            enriched = build_query_text(task, use_description=True)
            enriched_clean = preclean_for_fts(enriched)
            if enriched_clean.strip() and enriched_clean != query:
                try:
                    t_q = time.perf_counter()
                    alt_ranked, _, alt_total = hybrid_search(
                        enriched_clean, limit=args.fts_limit, reranker_override=adapter
                    )
                    lat = time.perf_counter() - t_q
                    if alt_total > total:
                        ranked = alt_ranked
                        total = alt_total
                        fallback_used = True
                except Exception:
                    pass

        dedup = dedup_by_repo(ranked)
        r10 = recall_at_k(dedup, expected, 10)
        recalls.append(r10)
        latencies.append(lat)
        per_ticket[tid] = {
            "recall_at_10": r10,
            "n_gt": len(expected),
            "top_10_repos": dedup[:10],
            "latency_s": lat,
            "total_candidates": total,
            "fallback_used": fallback_used,
        }
        # Print every ticket with flush=True so we can observe live progress
        # without depending on stdout line-buffering behaviour under shell
        # redirection.
        mean_so_far = sum(recalls) / len(recalls) if recalls else 0.0
        print(
            f"  {idx + 1}/{len(lost)} {tid}: lat={lat:.2f}s cand={total} r10={r10:.2f} "
            f"fallback={fallback_used} running_mean={mean_so_far:.4f}",
            flush=True,
        )

    elapsed = time.perf_counter() - t0
    mean_r10 = sum(recalls) / len(recalls) if recalls else 0.0
    hits = sum(1 for r in recalls if r > 0)

    print()
    print("=== A/B RESULT ===")
    print(f"flags: {flags}")
    print(f"n tickets evaluated: {len(recalls)}")
    print(f"r@10 mean: {mean_r10:.4f}")
    print(f"tickets with any hit: {hits}/{len(recalls)} ({100 * hits / max(len(recalls), 1):.1f}%)")
    print(f"wall time: {elapsed / 60:.1f} min")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "flags": flags,
                    "mean_r10": mean_r10,
                    "hits": hits,
                    "n": len(recalls),
                    "per_ticket": per_ticket,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"per-ticket snapshot: {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
