"""Paired bootstrap 95% CI for reranker bench deltas.

Per planning-debate convergence (.claude/debug/current/converged.md, 2026-04-26):
"$0 CI bootstrap on existing bench_runs/run*_*.json is STEP 1 before any more
training $." Three priors (pragmatist + systematist + refactorist) unanimously
adopt this as the gating action.

Method:
1. Load N bench JSONs (one per candidate, all on same eval set).
2. Match per-query rows by `query` string (paired bootstrap, not independent).
3. For each metric (recall@10, hit@5, hit@10, ndcg@10) and each candidate vs
   every other candidate, resample queries with replacement B times, recompute
   delta, take 2.5%/97.5% percentiles.
4. Print a per-pair, per-metric table with delta + 95% CI + sign verdict
   (POSITIVE / NEGATIVE / NOISE).
5. Optionally per-stratum (--per-stratum) — splits by `strata[0]` field.

Usage:
    python3 scripts/bootstrap_eval_ci.py \\
        --baseline=bench_runs/run1_rerank-l12_v3.json \\
        --candidates bench_runs/run1_rerank-mxbai_v3.json \\
                     bench_runs/run2_B_mxbai-docs-only.json \\
        --metric=recall_at_10 \\
        --bootstrap=10000

Output: stdout table + optional --json-out for downstream tools.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

DEFAULT_METRICS = ("recall_at_10", "hit_at_5", "hit_at_10", "ndcg_at_10")
DEFAULT_BOOTSTRAP = 10_000


def load_bench(path: Path) -> dict:
    data = json.loads(path.read_text())
    if "eval_per_query" not in data or not data["eval_per_query"]:
        sys.exit(f"FAIL {path}: no eval_per_query rows")
    return data


def paired_rows(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """Match by query string. Drops rows that don't appear in both."""
    by_q_a = {r["query"]: r for r in a["eval_per_query"]}
    by_q_b = {r["query"]: r for r in b["eval_per_query"]}
    common = sorted(set(by_q_a) & set(by_q_b))
    if not common:
        sys.exit("FAIL no overlapping queries between candidates")
    return [by_q_a[q] for q in common], [by_q_b[q] for q in common]


def bootstrap_delta_ci(
    rows_a: list[dict],
    rows_b: list[dict],
    metric: str,
    n_boot: int,
    seed: int = 42,
) -> dict:
    """Returns mean delta + 2.5/97.5 percentiles + sign."""
    rng = random.Random(seed)
    n = len(rows_a)
    if n != len(rows_b):
        raise ValueError("paired rows must be same length")

    deltas = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        sa = sum(rows_a[i].get(metric, 0) or 0 for i in idx) / n
        sb = sum(rows_b[i].get(metric, 0) or 0 for i in idx) / n
        deltas.append(sb - sa)

    deltas.sort()
    mean = statistics.mean(deltas)
    lo = deltas[int(0.025 * n_boot)]
    hi = deltas[int(0.975 * n_boot)]
    if hi < 0:
        verdict = "NEGATIVE"
    elif lo > 0:
        verdict = "POSITIVE"
    else:
        verdict = "NOISE"
    return {
        "mean_delta": mean,
        "ci_lo": lo,
        "ci_hi": hi,
        "verdict": verdict,
        "n": n,
        "n_boot": n_boot,
    }


def per_stratum_groups(rows_a: list[dict], rows_b: list[dict]) -> dict[str, tuple[list[dict], list[dict]]]:
    out: dict[str, tuple[list, list]] = {}
    for ra, rb in zip(rows_a, rows_b, strict=False):
        strata = ra.get("strata") or rb.get("strata") or ["unstratified"]
        for s in strata:
            out.setdefault(s, ([], []))[0].append(ra)
            out[s][1].append(rb)
    return out


def run(
    baseline: Path,
    candidates: list[Path],
    metrics: tuple[str, ...],
    n_boot: int,
    per_stratum: bool,
    json_out: Path | None,
) -> dict:
    base = load_bench(baseline)
    print(f"Baseline: {baseline.name}  (n_eval_rows={base.get('n_eval_rows')})")
    print(f"Bootstrap: {n_boot} resamples, paired by query")
    print()

    out_dump: dict = {"baseline": baseline.name, "candidates": {}}

    for cand in candidates:
        c = load_bench(cand)
        rows_b, rows_c = paired_rows(base, c)
        n_paired = len(rows_b)
        print(f"=== {cand.name}  (paired n={n_paired}) ===")

        cand_dump: dict = {"metrics": {}, "per_stratum": {}}

        # Aggregate metrics
        for m in metrics:
            r = bootstrap_delta_ci(rows_b, rows_c, m, n_boot)
            sign = "+" if r["mean_delta"] >= 0 else ""
            print(
                f"  {m:18s} Δ = {sign}{r['mean_delta']:+.4f}  "
                f"95% CI [{r['ci_lo']:+.4f}, {r['ci_hi']:+.4f}]  → {r['verdict']}"
            )
            cand_dump["metrics"][m] = r

        # Per-stratum (only for recall_at_10 by default to keep terse)
        if per_stratum:
            print("  --- per-stratum recall_at_10 ---")
            grp = per_stratum_groups(rows_b, rows_c)
            for stratum, (g_b, g_c) in sorted(grp.items()):
                if len(g_b) < 5:
                    continue  # too few queries for meaningful bootstrap
                r = bootstrap_delta_ci(g_b, g_c, "recall_at_10", n_boot)
                sign = "+" if r["mean_delta"] >= 0 else ""
                print(
                    f"    [{stratum:12s} n={len(g_b):3d}]  Δ = {sign}{r['mean_delta']:+.4f}  "
                    f"CI [{r['ci_lo']:+.4f}, {r['ci_hi']:+.4f}]  → {r['verdict']}"
                )
                cand_dump["per_stratum"][stratum] = {**r, "n_queries": len(g_b)}

        out_dump["candidates"][cand.name] = cand_dump
        print()

    if json_out:
        json_out.write_text(json.dumps(out_dump, indent=2))
        print(f"Wrote {json_out}")
    return out_dump


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", required=True, type=Path)
    p.add_argument("--candidates", required=True, type=Path, nargs="+")
    p.add_argument("--metrics", nargs="+", default=list(DEFAULT_METRICS))
    p.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP)
    p.add_argument("--per-stratum", action="store_true")
    p.add_argument("--json-out", type=Path)
    args = p.parse_args()

    if not args.baseline.is_file():
        sys.exit(f"baseline missing: {args.baseline}")
    for c in args.candidates:
        if not c.is_file():
            sys.exit(f"candidate missing: {c}")

    run(
        baseline=args.baseline,
        candidates=args.candidates,
        metrics=tuple(args.metrics),
        n_boot=args.bootstrap,
        per_stratum=args.per_stratum,
        json_out=args.json_out,
    )


if __name__ == "__main__":
    main()
