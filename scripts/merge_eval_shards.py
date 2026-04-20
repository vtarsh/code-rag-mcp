"""Merge N eval_finetune.py shard JSONs into one full history-out snapshot.

Expected inputs: <out>.shard0ofN.json ... <out>.shard(N-1)ofN.json
Each shard has per_task_baseline + per_task_ft_v1 + latencies. This script
unions them, re-computes aggregates/verdict using the same logic as
scripts/eval_finetune.py main() did when run single-process.

Usage:
  python3 scripts/merge_eval_shards.py \
    --shards profiles/pay-com/finetune_history/gte_v4.shard0of3.json \
             profiles/pay-com/finetune_history/gte_v4.shard1of3.json \
             profiles/pay-com/finetune_history/gte_v4.shard2of3.json \
    --manifest profiles/pay-com/finetune_data_v4/manifest.json \
    --out profiles/pay-com/finetune_history/gte_v4.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.eval_verdict import verdict_from_snapshot  # noqa: E402


def percentile(values: list, p: float) -> float:
    clean = [v for v in values if isinstance(v, (int, float))]
    if not clean:
        return 0.0
    s = sorted(clean)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return float(s[k])


def aggregate(per_task: dict, tickets: list[str]) -> dict:
    r10 = [per_task[t]["recall_at_10"] for t in tickets if t in per_task]
    r25 = [per_task[t]["recall_at_25"] for t in tickets if t in per_task]
    if not r10:
        return {"r10_mean": 0.0, "r25_mean": 0.0, "n": 0}
    return {
        "r10_mean": sum(r10) / len(r10),
        "r25_mean": sum(r25) / len(r25),
        "n": len(r10),
    }


def build_delta(base: dict, ft: dict) -> dict:
    """Build per-ticket delta dict. None-aware on rank_of_first_gt.

    Pre-2026-04-20 this used `(x or 999) - (y or 999)` which collapsed
    "both passes never found GT" (None, None) to delta=0 — invisible
    regression. Now we emit None when either side failed to find GT,
    matching eval_finetune.py's convention.
    """
    deltas: dict[str, dict] = {}
    for tid in set(base) & set(ft):
        b = base[tid]; f = ft[tid]
        b_rank = b.get("rank_of_first_gt")
        f_rank = f.get("rank_of_first_gt")
        rank_delta: int | None
        if b_rank is None or f_rank is None:
            rank_delta = None
        else:
            rank_delta = f_rank - b_rank
        deltas[tid] = {
            "recall_at_10": round(f["recall_at_10"] - b["recall_at_10"], 4),
            "recall_at_25": round(f["recall_at_25"] - b["recall_at_25"], 4),
            "rank_of_first_gt_delta": rank_delta,
        }
    return deltas


def find_regressions(deltas: dict, threshold: float = 0.05) -> dict:
    regressed = [
        {"ticket_id": tid, **d}
        for tid, d in deltas.items()
        if d["recall_at_10"] <= -threshold
    ]
    improved = [
        {"ticket_id": tid, **d}
        for tid, d in deltas.items()
        if d["recall_at_10"] >= threshold
    ]
    return {
        "tickets_regressed_ge5pp": regressed,
        "tickets_improved_ge5pp": improved,
        "n_regressed": len(regressed),
        "n_improved": len(improved),
    }


# `decide_verdict` lives in scripts/eval_verdict.py — single source of truth.
# Old gate here (max_regressions=3 on 909 tickets) was mathematically unworkable;
# audit 2026-04-20 replaced it with Δr@10 + ΔHit@5 + net_improved over the full
# eval set. See eval_verdict module docstring.


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=Path, nargs="+", required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    train_tickets: list[str] = list(manifest.get("train_tickets", []))
    test_tickets: list[str] = list(manifest.get("test_tickets", []))

    per_task_baseline: dict = {}
    per_task_ft: dict = {}
    lat_baseline: list[float] = []
    lat_ft: list[float] = []
    eval_config = None
    base_model = None
    ft_model_path = None
    hyperparams: dict = {}
    evaluated_tickets: list[str] = []

    for sp in args.shards:
        s = json.loads(sp.read_text(encoding="utf-8"))
        shard_base = s["per_task_baseline"]
        shard_ft = s["per_task_ft_v1"]

        # Fail fast on shard overlap — two shards covering the same ticket
        # would silently last-write-wins and double-count latencies.
        overlap_base = set(shard_base) & set(per_task_baseline)
        overlap_ft = set(shard_ft) & set(per_task_ft)
        if overlap_base or overlap_ft:
            raise ValueError(
                f"shard {sp.name} overlaps prior shards on tickets: "
                f"baseline={sorted(overlap_base)[:5]}..., ft={sorted(overlap_ft)[:5]}... "
                f"(shard stride in eval_finetune.py is seeded+deterministic — "
                f"overlap means re-run of one shard or mismatched shard set)"
            )

        per_task_baseline.update(shard_base)
        per_task_ft.update(shard_ft)
        lat_baseline.extend(s.get("latency_baseline", []))
        lat_ft.extend(s.get("latency_ft_v1", []))
        evaluated_tickets.extend(s.get("evaluated_tickets", []))
        if eval_config is None:
            eval_config = s["eval_config"]
            base_model = s["base_model"]
            ft_model_path = s["ft_model_path"]
            hyperparams = s.get("hyperparams", {})
        print(f"merged shard {sp.name}: +{len(shard_base)} tickets", file=sys.stderr)

    # Dedupe evaluated_tickets in case a shard snapshot was regenerated with
    # overlap on a prior run (disjoint check above already caught per_task
    # collisions, but evaluated_tickets is extended first via concat).
    evaluated_tickets = sorted(set(evaluated_tickets))

    train_in_eval = [t for t in train_tickets if t in evaluated_tickets]
    test_in_eval = [t for t in test_tickets if t in evaluated_tickets]

    agg_base_train = aggregate(per_task_baseline, train_in_eval)
    agg_base_test = aggregate(per_task_baseline, test_in_eval)
    agg_ft_train = aggregate(per_task_ft, train_in_eval)
    agg_ft_test = aggregate(per_task_ft, test_in_eval)

    agg = {
        "baseline": {
            "r10_mean_train": round(agg_base_train["r10_mean"], 4),
            "r25_mean_train": round(agg_base_train["r25_mean"], 4),
            "r10_mean_test": round(agg_base_test["r10_mean"], 4),
            "r25_mean_test": round(agg_base_test["r25_mean"], 4),
            "n_train_evaluated": agg_base_train["n"],
            "n_test_evaluated": agg_base_test["n"],
        },
        "ft_v1": {
            "r10_mean_train": round(agg_ft_train["r10_mean"], 4),
            "r25_mean_train": round(agg_ft_train["r25_mean"], 4),
            "r10_mean_test": round(agg_ft_test["r10_mean"], 4),
            "r25_mean_test": round(agg_ft_test["r25_mean"], 4),
            "n_train_evaluated": agg_ft_train["n"],
            "n_test_evaluated": agg_ft_test["n"],
        },
        "delta_train": {
            "r10": round(agg_ft_train["r10_mean"] - agg_base_train["r10_mean"], 4),
            "r25": round(agg_ft_train["r25_mean"] - agg_base_train["r25_mean"], 4),
        },
        "delta_test": {
            "r10": round(agg_ft_test["r10_mean"] - agg_base_test["r10_mean"], 4),
            "r25": round(agg_ft_test["r25_mean"] - agg_base_test["r25_mean"], 4),
        },
    }

    per_task_delta = build_delta(per_task_baseline, per_task_ft)
    regressions_all = find_regressions(per_task_delta)
    verdict_result = verdict_from_snapshot(per_task_baseline, per_task_ft, per_task_delta)
    verdict = verdict_result.verdict
    reason = verdict_result.reason

    snapshot = {
        "run_id": args.out.stem,
        "base_model": base_model,
        "ft_model_path": ft_model_path,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merged_from_shards": [str(s) for s in args.shards],
        "hyperparams": hyperparams,
        "eval_config": eval_config,
        "train_tickets": train_tickets,
        "test_tickets": test_tickets,
        "evaluated_tickets": evaluated_tickets,
        "per_task_baseline": per_task_baseline,
        "per_task_ft_v1": per_task_ft,
        "per_task_delta": per_task_delta,
        "aggregate": agg,
        "regressions": regressions_all,
        "latency": {
            "baseline_p50_s": round(percentile(lat_baseline, 50), 3),
            "baseline_p95_s": round(percentile(lat_baseline, 95), 3),
            "ft_v1_p50_s": round(percentile(lat_ft, 50), 3),
            "ft_v1_p95_s": round(percentile(lat_ft, 95), 3),
        },
        "verdict": verdict,
        "verdict_reason": reason,
        "verdict_metrics": verdict_result.metrics,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(snapshot, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"WROTE {args.out}", file=sys.stderr)
    print(f"  total tickets evaluated: {len(per_task_ft)}")
    print(f"  delta_test r@10: {agg['delta_test']['r10']:+.4f}")
    print(f"  verdict: {verdict} ({reason})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
