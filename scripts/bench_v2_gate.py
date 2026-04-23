#!/usr/bin/env python3
"""Regression gate for ``bench_v2`` runs (proposal §5).

Compares the current ``bench_v2`` result JSON against the pinned baseline.
Per stratum (``overall`` + by intent + by length + by top-5 providers):

  * Δ file_recall@10 < -0.02  -> print offending stratum(s) + exit 1.
  * Any ``answerable==no`` row counted as a hit -> exit 1 (hygiene check).

Override:

  --accept-regression --reason="<text>"  appends an entry to
  ``profiles/pay-com/RECALL-TRACKER.md`` and exits 0.

Input JSON schema (emitted by ``benchmark_bench_v2.py``)::

    {
      "timestamp": "...",
      "overall": {"file_recall@10": 0.46, "count": 120},
      "strata": {
        "intent:code":       {"file_recall@10": 0.50, "count": 70},
        "length:medium":     {"file_recall@10": 0.48, "count": 90},
        "provider:payper":   {"file_recall@10": 0.55, "count": 22},
        ...
      },
      "hygiene": {"unanswerable_hits": 0}
    }
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Optional

REGRESSION_THRESHOLD = -0.02  # Δ file_recall@10 below this is a fail.


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _get_recall(block: dict) -> Optional[float]:
    if not isinstance(block, dict):
        return None
    v = block.get("file_recall@10")
    return float(v) if v is not None else None


def compare(current: dict, baseline: dict) -> tuple[list[dict], list[str]]:
    """Return (regressions, hygiene_violations).

    Each regression is ``{stratum, baseline, current, delta}``.
    """
    regressions: list[dict] = []
    # Overall
    base_over = _get_recall(baseline.get("overall") or {})
    cur_over = _get_recall(current.get("overall") or {})
    if base_over is not None and cur_over is not None:
        delta = cur_over - base_over
        if delta < REGRESSION_THRESHOLD:
            regressions.append(
                {
                    "stratum": "overall",
                    "baseline": base_over,
                    "current": cur_over,
                    "delta": delta,
                }
            )

    # Per-stratum
    base_strata = baseline.get("strata") or {}
    cur_strata = current.get("strata") or {}
    for name, base_block in base_strata.items():
        base_v = _get_recall(base_block)
        cur_block = cur_strata.get(name) or {}
        cur_v = _get_recall(cur_block)
        if base_v is None or cur_v is None:
            continue
        delta = cur_v - base_v
        if delta < REGRESSION_THRESHOLD:
            regressions.append(
                {
                    "stratum": name,
                    "baseline": base_v,
                    "current": cur_v,
                    "delta": delta,
                }
            )

    # Hygiene: unanswerable queries counted as hits are always a fail, even
    # without a baseline.
    hygiene: list[str] = []
    hyg_block = current.get("hygiene") or {}
    unhit = hyg_block.get("unanswerable_hits")
    if isinstance(unhit, int) and unhit > 0:
        hygiene.append(
            f"{unhit} row(s) with answerable=no were counted as hits — dataset hygiene violation"
        )

    return regressions, hygiene


def _format_regression_report(regressions: list[dict], hygiene: list[str]) -> str:
    lines: list[str] = []
    if regressions:
        lines.append("REGRESSION — file_recall@10 dropped on strata:")
        for r in regressions:
            lines.append(
                f"  {r['stratum']:<24} baseline={r['baseline']:.3f} "
                f"current={r['current']:.3f} delta={r['delta']:+.3f}"
            )
    if hygiene:
        lines.append("HYGIENE:")
        for h in hygiene:
            lines.append(f"  {h}")
    return "\n".join(lines)


def append_tracker_entry(
    tracker_path: Path, *, current_path: Path, reason: str, regressions: list[dict]
) -> None:
    """Append a ``--accept-regression`` justification to RECALL-TRACKER.md."""
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "",
        f"## bench_v2 accepted regression — {stamp}",
        f"- **Result:** `{current_path}`",
        f"- **Reason:** {reason}",
    ]
    if regressions:
        lines.append("- **Regressions accepted:**")
        for r in regressions:
            lines.append(
                f"  - `{r['stratum']}`: {r['baseline']:.3f} -> {r['current']:.3f} "
                f"({r['delta']:+.3f})"
            )
    lines.append("")
    with tracker_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--current", type=Path, required=True, help="Path to current bench_v2 results JSON")
    p.add_argument(
        "--baseline",
        type=Path,
        default=Path("profiles/pay-com/bench/baselines/bench_v2.latest.json"),
        help="Path to pinned baseline JSON",
    )
    p.add_argument(
        "--tracker",
        type=Path,
        default=Path("profiles/pay-com/RECALL-TRACKER.md"),
        help="Tracker to append on --accept-regression",
    )
    p.add_argument("--accept-regression", action="store_true")
    p.add_argument("--reason", type=str, default="")
    args = p.parse_args(argv)

    if not args.current.exists():
        print(f"ERROR: current results not found: {args.current}", file=sys.stderr)
        return 2
    current = _load_json(args.current)

    if not args.baseline.exists():
        # Treat missing baseline as "no baseline yet — current becomes baseline".
        # Still enforce hygiene check though.
        print(f"NOTE: baseline {args.baseline} not found — only hygiene check enforced.")
        _, hygiene = compare(current, {})
        if hygiene:
            print(_format_regression_report([], hygiene))
            return 1
        print("PASS (no baseline, no hygiene issues).")
        return 0

    baseline = _load_json(args.baseline)
    regressions, hygiene = compare(current, baseline)

    if not regressions and not hygiene:
        print("PASS — no per-stratum regression beyond threshold, no hygiene issues.")
        return 0

    report = _format_regression_report(regressions, hygiene)
    print(report)

    if regressions and args.accept_regression:
        if not args.reason.strip():
            print("ERROR: --accept-regression requires --reason=<text>", file=sys.stderr)
            return 2
        # Hygiene is non-overridable — zero new unanswerable-hits is absolute.
        if hygiene:
            print("ERROR: hygiene violations cannot be overridden", file=sys.stderr)
            return 1
        append_tracker_entry(
            args.tracker,
            current_path=args.current,
            reason=args.reason,
            regressions=regressions,
        )
        print(f"OVERRIDE accepted — appended to {args.tracker}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
