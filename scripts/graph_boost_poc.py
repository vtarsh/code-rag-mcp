"""Offline POC: does a graph-neighbor boost on top-200 FTS candidates
improve repo-level r@10 on low-recall tickets?

Fair A/B: for each of 100 low-recall tickets from v6.2 snapshot, we
  1. fetch top-200 FTS chunks (same fetch_fts_candidates as eval_finetune).
  2. compute BASELINE repo ranking: dedupe-by-repo, score = 1/(fts_rank+K),
     take top-25 repos.
  3. compute BOOSTED ranking: the same, but add +alpha to the score of any
     candidate whose repo is a 1-hop neighbor (via graph_edges) of the
     top-N seed repos in the baseline — excluding hub repos (degree > cutoff)
     and metadata-only edge types.
  4. score r@10, r@25, Hit@5 on baseline vs boosted. Report deltas.

No reranker loaded — this is a signal test: does graph add information on
top of pure FTS ordering? A positive signal here justifies the harder POC
of wiring it into the production rerank pipeline.

Usage:
    python3.12 scripts/graph_boost_poc.py              # default sweep
    python3.12 scripts/graph_boost_poc.py --alphas 0.2 --hub-cutoffs 150
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.benchmark_rerank_ab import (  # noqa: E402
    fetch_fts_candidates,
)
from scripts.eval_verdict import mean  # noqa: E402


_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
TASKS_DB = _BASE / "db" / "tasks.db"
KNOWLEDGE_DB = _BASE / "db" / "knowledge.db"
HISTORY = _BASE / "profiles" / "pay-com" / "finetune_history" / "gte_v6_2.json"
TICKET_LIST = Path("/tmp/low_recall_100.json")

# RRF-like constant for baseline scoring (same spirit as hybrid.py).
K_RRF = 60

# Edge types treated as retrieval signal. Metadata-only types excluded:
# proto_message_def/usage, proto_service_def, npm_dep_tooling (tooling-only),
# similar_repo (already consumed elsewhere).
USEFUL_EDGE_TYPES = (
    "npm_dep",
    "grpc_method_call",
    "grpc_client_usage",
    "proto_import",
    "grpc_call",
    "express_route",
    "temporal_activate",
    "child_workflow",
    "signal_handler",
    "webhook_dispatch",
    "webhook_handler",
    "runtime_routing",
    "url_reference",
    "express_mount",
    "workflow_import",
)

# Seed count: we use the top-N baseline repos as seeds for neighbor expansion.
SEED_K = 5


def load_ticket_records(ticket_ids: list[str]) -> list[dict]:
    """Load (ticket_id, summary, expected_repos) for a given list of tickets."""
    conn = sqlite3.connect(str(TASKS_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    placeholders = ",".join("?" * len(ticket_ids))
    rows = conn.execute(
        f"SELECT ticket_id, summary, repos_changed FROM task_history "
        f"WHERE ticket_id IN ({placeholders})",
        ticket_ids,
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            expected = json.loads(r["repos_changed"] or "[]")
        except (TypeError, json.JSONDecodeError):
            expected = []
        if not expected:
            continue
        out.append({
            "ticket_id": r["ticket_id"],
            "summary": r["summary"] or "",
            "expected_repos": list(expected),
        })
    return out


def load_graph_neighbors(edge_types: tuple[str, ...]) -> tuple[dict[str, set[str]], dict[str, int]]:
    """Return (neighbors, degrees).

    neighbors[repo] = set of repos reachable in 1 hop via any of the given
    edge types (bidirectional — either as source or target).
    degrees[repo] = total degree (in + out) on useful-edge subset, used for
    hub filtering.
    """
    conn = sqlite3.connect(str(KNOWLEDGE_DB), timeout=30)
    conn.execute("PRAGMA query_only = ON")
    placeholders = ",".join("?" * len(edge_types))
    rows = conn.execute(
        f"SELECT source, target FROM graph_edges WHERE edge_type IN ({placeholders})",
        list(edge_types),
    ).fetchall()
    conn.close()

    neighbors: dict[str, set[str]] = defaultdict(set)
    degrees: dict[str, int] = defaultdict(int)
    for src, tgt in rows:
        if not src or not tgt or src == tgt:
            continue
        neighbors[src].add(tgt)
        neighbors[tgt].add(src)
        degrees[src] += 1
        degrees[tgt] += 1
    return dict(neighbors), dict(degrees)


def score_candidates_rrf(chunks: list[dict]) -> dict[str, float]:
    """RRF-like scoring by first-occurrence rank per repo.

    Score for repo = sum over its chunks of 1/(K + chunk_rank). Higher = better.
    This is monotonic with "best chunk rank" in FTS order and cheap.
    """
    scores: dict[str, float] = defaultdict(float)
    for rank, ch in enumerate(chunks):
        repo = ch.get("repo_name", "")
        if not repo:
            continue
        scores[repo] += 1.0 / (K_RRF + rank + 1)
    return dict(scores)


def top_k_repos_from_scores(scores: dict[str, float], k: int) -> list[str]:
    return [r for r, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]]


def apply_graph_boost(
    baseline_scores: dict[str, float],
    seed_repos: list[str],
    neighbors: dict[str, set[str]],
    degrees: dict[str, int],
    *,
    alpha: float,
    hub_cutoff: int,
) -> dict[str, float]:
    """Add alpha to the score of any repo that is a 1-hop neighbor of a seed
    AND is not itself a hub (total degree > hub_cutoff) AND is already a
    candidate in baseline_scores (we only re-rank, we don't inject unseen).
    """
    seed_neighbors: set[str] = set()
    for seed in seed_repos:
        for nb in neighbors.get(seed, ()):
            if nb == seed:
                continue
            if degrees.get(nb, 0) > hub_cutoff:
                continue
            seed_neighbors.add(nb)

    boosted = dict(baseline_scores)  # copy
    for repo in seed_neighbors:
        if repo in boosted:
            boosted[repo] += alpha
    return boosted


def compute_recall(ranked: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 0.0
    return len(expected & set(ranked[:k])) / len(expected)


def hit_at(ranked: list[str], expected: set[str], k: int) -> float:
    return 1.0 if any(r in expected for r in ranked[:k]) else 0.0


def rank_of_first_hit(ranked: list[str], expected: set[str]) -> int | None:
    for i, r in enumerate(ranked, start=1):
        if r in expected:
            return i
    return None


def mrr_at_10(rank: int | None) -> float:
    if rank is None or rank > 10:
        return 0.0
    return 1.0 / rank


def run_one_config(
    tasks: list[dict],
    fts_conn: sqlite3.Connection,
    neighbors: dict[str, set[str]],
    degrees: dict[str, int],
    *,
    fts_limit: int,
    alpha: float,
    hub_cutoff: int,
) -> dict:
    """Return aggregate metrics + per-task table for one (alpha, hub_cutoff)."""
    r10_base: list[float] = []
    r10_boost: list[float] = []
    r25_base: list[float] = []
    r25_boost: list[float] = []
    hit5_base: list[float] = []
    hit5_boost: list[float] = []
    mrr_base: list[float] = []
    mrr_boost: list[float] = []
    per_task: list[dict] = []
    no_candidates = 0

    for task in tasks:
        expected = set(task["expected_repos"])
        try:
            chunks = fetch_fts_candidates(fts_conn, task["summary"], limit=fts_limit)
        except ValueError:
            chunks = []
        if not chunks:
            no_candidates += 1
            continue

        base_scores = score_candidates_rrf(chunks)
        base_ranking = top_k_repos_from_scores(base_scores, 25)
        seeds = base_ranking[:SEED_K]

        boost_scores = apply_graph_boost(
            base_scores, seeds, neighbors, degrees,
            alpha=alpha, hub_cutoff=hub_cutoff,
        )
        boost_ranking = top_k_repos_from_scores(boost_scores, 25)

        # Metrics
        r10_b = compute_recall(base_ranking, expected, 10)
        r10_o = compute_recall(boost_ranking, expected, 10)
        r25_b = compute_recall(base_ranking, expected, 25)
        r25_o = compute_recall(boost_ranking, expected, 25)
        h5_b = hit_at(base_ranking, expected, 5)
        h5_o = hit_at(boost_ranking, expected, 5)
        m_b = mrr_at_10(rank_of_first_hit(base_ranking, expected))
        m_o = mrr_at_10(rank_of_first_hit(boost_ranking, expected))

        r10_base.append(r10_b)
        r10_boost.append(r10_o)
        r25_base.append(r25_b)
        r25_boost.append(r25_o)
        hit5_base.append(h5_b)
        hit5_boost.append(h5_o)
        mrr_base.append(m_b)
        mrr_boost.append(m_o)

        per_task.append({
            "ticket_id": task["ticket_id"],
            "n_gt": len(expected),
            "r10_delta": round(r10_o - r10_b, 4),
            "hit5_delta": round(h5_o - h5_b, 4),
            "mrr_delta": round(m_o - m_b, 4),
        })

    n_imp_r10 = sum(1 for t in per_task if t["r10_delta"] >= 0.05)
    n_reg_r10 = sum(1 for t in per_task if t["r10_delta"] <= -0.05)
    n_imp_mrr = sum(1 for t in per_task if t["mrr_delta"] >= 0.05)
    n_reg_mrr = sum(1 for t in per_task if t["mrr_delta"] <= -0.05)

    return {
        "config": {"alpha": alpha, "hub_cutoff": hub_cutoff, "fts_limit": fts_limit},
        "n_tickets_evaluated": len(per_task),
        "n_no_candidates": no_candidates,
        "r10_mean_base": round(mean(r10_base), 4),
        "r10_mean_boost": round(mean(r10_boost), 4),
        "r10_delta_mean": round(mean(r10_boost) - mean(r10_base), 4),
        "r25_delta_mean": round(mean(r25_boost) - mean(r25_base), 4),
        "hit5_delta_mean": round(mean(hit5_boost) - mean(hit5_base), 4),
        "mrr_delta_mean": round(mean(mrr_boost) - mean(mrr_base), 4),
        "n_improved_r10": n_imp_r10,
        "n_regressed_r10": n_reg_r10,
        "net_improved_r10": n_imp_r10 - n_reg_r10,
        "n_improved_mrr": n_imp_mrr,
        "n_regressed_mrr": n_reg_mrr,
        "per_task": per_task,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--tickets-file", type=Path, default=TICKET_LIST)
    p.add_argument("--fts-limit", type=int, default=200)
    p.add_argument("--alphas", type=float, nargs="+",
                   default=[0.02, 0.05, 0.1, 0.2])
    p.add_argument("--hub-cutoffs", type=int, nargs="+",
                   default=[100, 150, 250])
    p.add_argument("--out", type=Path,
                   default=_BASE / "profiles" / "pay-com" / "finetune_history" / "graph_boost_poc.json")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    ticket_ids = json.loads(args.tickets_file.read_text())
    tasks = load_ticket_records(ticket_ids)
    print(f"loaded {len(tasks)} tickets (of {len(ticket_ids)} requested)")

    neighbors, degrees = load_graph_neighbors(USEFUL_EDGE_TYPES)
    print(f"graph: {len(neighbors)} repos with neighbors, "
          f"total edges considered={sum(len(v) for v in neighbors.values()) // 2}")
    # Top hubs for reference:
    top_hubs = sorted(degrees.items(), key=lambda x: x[1], reverse=True)[:10]
    print("top-10 hubs (repo, degree):")
    for repo, deg in top_hubs:
        print(f"  {repo}: {deg}")

    fts_conn = sqlite3.connect(str(KNOWLEDGE_DB), timeout=30)
    fts_conn.row_factory = sqlite3.Row
    fts_conn.execute("PRAGMA query_only = ON")

    results: list[dict] = []
    try:
        for alpha in args.alphas:
            for hub in args.hub_cutoffs:
                res = run_one_config(
                    tasks, fts_conn, neighbors, degrees,
                    fts_limit=args.fts_limit, alpha=alpha, hub_cutoff=hub,
                )
                print(f"\n=== α={alpha}, hub_cutoff={hub} ===")
                print(f"  r@10 base→boost = {res['r10_mean_base']:.4f} → "
                      f"{res['r10_mean_boost']:.4f} (Δ{res['r10_delta_mean']:+.4f})")
                print(f"  r@25 Δ={res['r25_delta_mean']:+.4f}, "
                      f"Hit@5 Δ={res['hit5_delta_mean']:+.4f}, "
                      f"MRR Δ={res['mrr_delta_mean']:+.4f}")
                print(f"  r@10 improved/regressed: {res['n_improved_r10']} / {res['n_regressed_r10']}"
                      f" (net={res['net_improved_r10']:+d})")
                print(f"  MRR improved/regressed:  {res['n_improved_mrr']} / {res['n_regressed_mrr']}")
                results.append(res)
    finally:
        fts_conn.close()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"results": results, "n_tasks": len(tasks),
                    "edge_types": list(USEFUL_EDGE_TYPES),
                    "seed_k": SEED_K, "k_rrf": K_RRF},
                   indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out}")

    # Summary: best config by Δr@10 with MRR-neutral gate.
    print("\n=== Summary: configs passing 'MRR not regressed' ===")
    passing = [r for r in results if r["mrr_delta_mean"] >= -0.005]
    passing.sort(key=lambda r: r["r10_delta_mean"], reverse=True)
    for r in passing[:5]:
        cfg = r["config"]
        print(f"  α={cfg['alpha']:.2f} hub={cfg['hub_cutoff']:>3}: "
              f"Δr@10={r['r10_delta_mean']:+.4f}, "
              f"ΔHit@5={r['hit5_delta_mean']:+.4f}, "
              f"ΔMRR={r['mrr_delta_mean']:+.4f}, "
              f"net={r['net_improved_r10']:+d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
