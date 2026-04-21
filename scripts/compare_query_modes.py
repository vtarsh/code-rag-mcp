#!/usr/bin/env python3
"""Compare two eval snapshots that differ only in eval_query_mode.

Input: two history_out JSONs from scripts/eval_finetune.py — one run with
--eval-query-mode=summary, one with --eval-query-mode=enriched.

Output: per-project delta (r@10, Hit@5) between the two modes, for both
baseline and FT model. Answers "does enriched query composition help, and
which of baseline/FT benefits more?" — the direct test for the train/eval
query mismatch hypothesis (critic B, 2026-04-20 night synthesis).
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hit5(per_task: dict) -> dict[str, int]:
    # Hit@5 = 1 if rank_of_first_gt <= 5 and not None else 0.
    out = {}
    for tid, row in per_task.items():
        rank = row.get("rank_of_first_gt")
        out[tid] = 1 if (rank is not None and rank <= 5) else 0
    return out


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def per_project_delta(summary_snap: dict, enriched_snap: dict, key: str) -> list[dict]:
    """key = 'per_task_baseline' or 'per_task_ft_v1'."""
    sum_pt = summary_snap.get(key, {})
    enr_pt = enriched_snap.get(key, {})
    common = sorted(set(sum_pt) & set(enr_pt))

    sum_h5 = _hit5(sum_pt)
    enr_h5 = _hit5(enr_pt)

    groups: dict[str, list[str]] = defaultdict(list)
    for tid in common:
        prefix = tid.split("-", 1)[0]
        groups[prefix].append(tid)

    out = []
    for prefix in [*sorted(groups), "ALL"]:
        tids = common if prefix == "ALL" else groups[prefix]
        if not tids:
            continue
        r10_sum = mean([sum_pt[t]["recall_at_10"] for t in tids])
        r10_enr = mean([enr_pt[t]["recall_at_10"] for t in tids])
        h5_sum = mean([sum_h5[t] for t in tids])
        h5_enr = mean([enr_h5[t] for t in tids])
        improved_r10 = sum(1 for t in tids if enr_pt[t]["recall_at_10"] > sum_pt[t]["recall_at_10"])
        regressed_r10 = sum(1 for t in tids if enr_pt[t]["recall_at_10"] < sum_pt[t]["recall_at_10"])
        out.append(
            {
                "project": prefix,
                "n": len(tids),
                "r10_summary": r10_sum,
                "r10_enriched": r10_enr,
                "delta_r10": r10_enr - r10_sum,
                "h5_summary": h5_sum,
                "h5_enriched": h5_enr,
                "delta_h5": h5_enr - h5_sum,
                "improved_r10": improved_r10,
                "regressed_r10": regressed_r10,
                "net_r10": improved_r10 - regressed_r10,
            }
        )
    return out


def render_table(rows: list[dict], label: str) -> str:
    header = (
        f"\n### {label}\n"
        "| project | n | r@10 sum | r@10 enr | Δr@10 | H@5 sum | H@5 enr | ΔH@5 | imp | reg | net |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    body = ""
    for r in rows:
        body += (
            f"| {r['project']} | {r['n']} | "
            f"{r['r10_summary']:.4f} | {r['r10_enriched']:.4f} | {r['delta_r10']:+.4f} | "
            f"{r['h5_summary']:.4f} | {r['h5_enriched']:.4f} | {r['delta_h5']:+.4f} | "
            f"{r['improved_r10']} | {r['regressed_r10']} | {r['net_r10']:+d} |\n"
        )
    return header + body


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--summary", required=True, type=Path, help="eval snapshot JSON run with --eval-query-mode=summary")
    p.add_argument(
        "--enriched", required=True, type=Path, help="eval snapshot JSON run with --eval-query-mode=enriched"
    )
    p.add_argument("--out", type=Path, default=None, help="Optional markdown output path")
    args = p.parse_args()

    s = load_snapshot(args.summary)
    e = load_snapshot(args.enriched)

    s_mode = (s.get("eval_config") or {}).get("query_mode")
    e_mode = (e.get("eval_config") or {}).get("query_mode")
    print(f"summary snapshot query_mode = {s_mode!r}")
    print(f"enriched snapshot query_mode = {e_mode!r}")

    baseline_rows = per_project_delta(s, e, "per_task_baseline")
    ft_rows = per_project_delta(s, e, "per_task_ft_v1")

    md = f"# Query-mode compare: {args.summary.name} vs {args.enriched.name}\n"
    md += render_table(baseline_rows, "Baseline (L6 ms-marco-MiniLM-L-6-v2)")
    md += render_table(ft_rows, "FT model (as per history snapshot)")

    md += "\n### Interpretation\n"
    baseline_delta = next(r for r in baseline_rows if r["project"] == "ALL")["delta_r10"]
    ft_delta = next(r for r in ft_rows if r["project"] == "ALL")["delta_r10"]
    gain_on_ft = ft_delta - baseline_delta
    md += f"- ALL-tickets Δr@10 baseline: **{baseline_delta:+.4f}**\n"
    md += f"- ALL-tickets Δr@10 FT: **{ft_delta:+.4f}**\n"
    md += f"- Query-parity gain (FT Delta - baseline Delta): **{gain_on_ft:+.4f}**\n"
    if gain_on_ft > 0.01:
        md += "- **HYPOTHESIS CONFIRMED**: FT benefits MORE from enriched queries than baseline does. "
        md += "Train/eval query mismatch was a real bottleneck.\n"
    elif gain_on_ft < -0.01:
        md += "- **HYPOTHESIS REFUTED**: baseline benefits more than FT. Query mismatch not the root cause.\n"
    else:
        md += "- **INCONCLUSIVE**: delta-of-deltas within noise floor (|±0.01|). Mismatch unlikely dominant.\n"

    print(md)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(md, encoding="utf-8")
        print(f"\nWritten: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
