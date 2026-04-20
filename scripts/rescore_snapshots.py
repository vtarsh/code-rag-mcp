"""Re-score historical eval snapshots with the new (post-2026-04-20) verdict gate.

Reads each gte_v*.json in profiles/pay-com/finetune_history/, runs
scripts/eval_verdict.verdict_from_snapshot() on the stored per_task dicts,
prints a comparison table (OLD verdict vs NEW verdict) and writes a
markdown summary to finetune_history/rescore_<today>.md.

Also rewrites each snapshot in-place with the new verdict + verdict_metrics
so downstream tooling (ROADMAP, benchmarks) sees consistent values.

Usage:
    python3.12 scripts/rescore_snapshots.py            # dry-run, print only
    python3.12 scripts/rescore_snapshots.py --write    # overwrite snapshots
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.eval_verdict import verdict_from_snapshot  # noqa: E402


_HISTORY = Path(__file__).resolve().parent.parent / "profiles" / "pay-com" / "finetune_history"


def rescore_one(snapshot_path: Path) -> dict:
    snap = json.loads(snapshot_path.read_text(encoding="utf-8"))
    old_verdict = snap.get("verdict", "(none)")
    old_reason = snap.get("verdict_reason", "")

    result = verdict_from_snapshot(
        snap["per_task_baseline"],
        snap["per_task_ft_v1"],
        snap.get("per_task_delta") or {},
    )
    return {
        "path": snapshot_path,
        "run_id": snap.get("run_id", snapshot_path.stem),
        "old_verdict": old_verdict,
        "old_reason": old_reason,
        "new_verdict": result.verdict,
        "new_reason": result.reason,
        "new_metrics": result.metrics,
        "snap": snap,
    }


def apply_rewrite(record: dict) -> None:
    snap = record["snap"]
    snap["verdict"] = record["new_verdict"]
    snap["verdict_reason"] = record["new_reason"]
    snap["verdict_metrics"] = record["new_metrics"]
    record["path"].write_text(
        json.dumps(snap, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )


def fmt_table(records: list[dict]) -> str:
    lines = [
        "| run | old verdict | new verdict | Δr@10 | ΔHit@5 | improved | regressed | net |",
        "|-----|-------------|-------------|-------|--------|----------|-----------|-----|",
    ]
    for r in records:
        m = r["new_metrics"]
        lines.append(
            f"| {r['run_id']} | {r['old_verdict']} | **{r['new_verdict']}** | "
            f"{m['delta_r10_all']:+.3f} | {m['delta_hit5_all']:+.3f} | "
            f"{m['n_improved_r10']} | {m['n_regressed_r10']} | "
            f"{m['net_improved_r10']:+d} |"
        )
    return "\n".join(lines)


def fmt_diagnostic_table(records: list[dict]) -> str:
    lines = [
        "| run | r@10 base → ft | Hit@5 base → ft | MRR@10 base → ft (diag) |",
        "|-----|---------------|-----------------|-------------------------|",
    ]
    for r in records:
        m = r["new_metrics"]
        lines.append(
            f"| {r['run_id']} | {m['r10_baseline']:.4f} → {m['r10_ft']:.4f} "
            f"| {m['hit5_baseline']:.4f} → {m['hit5_ft']:.4f} "
            f"| {m['mrr_baseline_diag']:.4f} → {m['mrr_ft_diag']:.4f} "
            f"(Δ{m['delta_mrr_diag']:+.4f}) |"
        )
    return "\n".join(lines)


def fmt_summary_md(records: list[dict]) -> str:
    today = date.today().isoformat()
    promoted = [r for r in records if r["new_verdict"] == "PROMOTE"]
    held = [r for r in records if r["new_verdict"] == "HOLD"]
    rejected = [r for r in records if r["new_verdict"] == "REJECT"]

    parts = [
        f"# Snapshot re-score — {today}",
        "",
        "Re-scoring existing historical eval snapshots with the post-audit",
        "verdict gate (scripts/eval_verdict.py). Old gate was mathematically",
        "unworkable (n_regressions ≤ 3 on 909 tickets with 46% n_gt=1 binary).",
        "",
        "## Verdict comparison",
        "",
        fmt_table(records),
        "",
        "## Diagnostic metrics (MRR is NOT in gate — shown for reference only)",
        "",
        fmt_diagnostic_table(records),
        "",
        "## Summary",
        "",
        f"- **PROMOTE**: {len(promoted)} ({', '.join(r['run_id'] for r in promoted) or 'none'})",
        f"- **HOLD**:    {len(held)} ({', '.join(r['run_id'] for r in held) or 'none'})",
        f"- **REJECT**:  {len(rejected)} ({', '.join(r['run_id'] for r in rejected) or 'none'})",
        "",
        "## Interpretation",
        "",
        "The new gate is necessary but NOT sufficient for production promotion.",
        "A PROMOTE on this gate means the FT model has consistent improvement on",
        "Jira-sourced eval (r@10 + Hit@5 + counts). It does NOT confirm:",
        "",
        "- Latency parity (v7 was 2× slower than v6.2 despite passing Jira eval)",
        "- Runtime query distribution performance (see benchmark_queries.py /",
        "  benchmark_realworld.py — they measure different intent patterns)",
        "- Per-project parity (CORE or BO regression hidden in aggregate)",
        "",
        "Use this gate as a gate on FT iteration cycles, pair with runtime",
        "benchmarks before any production swap.",
    ]
    return "\n".join(parts)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true",
                   help="Also rewrite snapshot JSONs in place with new verdict.")
    p.add_argument("--history-dir", type=Path, default=_HISTORY)
    p.add_argument("--out-md", type=Path, default=None,
                   help="Path for markdown summary. Default: finetune_history/rescore_<today>.md")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    # Exclude shard partials (e.g. gte_v8.shard0of3.json) — only merged snapshots
    snapshots = sorted(
        p for p in args.history_dir.glob("gte_v*.json")
        if ".shard" not in p.name
    )
    if not snapshots:
        print(f"no snapshots in {args.history_dir}", file=sys.stderr)
        return 1

    records = [rescore_one(p) for p in snapshots]

    summary = fmt_summary_md(records)
    print(summary)

    out_path = args.out_md or args.history_dir / f"rescore_{date.today().isoformat()}.md"
    out_path.write_text(summary, encoding="utf-8")
    print(f"\nwrote summary → {out_path}", file=sys.stderr)

    if args.write:
        for r in records:
            apply_rewrite(r)
            print(f"rewrote {r['path'].name}", file=sys.stderr)
    else:
        print("\n(dry-run — pass --write to rewrite snapshot JSONs)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
