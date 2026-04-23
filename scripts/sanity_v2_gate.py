#!/usr/bin/env python3
"""Sanity-check v1 vs v2 verdict on existing snapshot pairs.

Per proposal §7. Loads N≥2 snapshot files (each = ``{per_task_baseline,
per_task_ft_v1, …}`` JSON written by ``eval_finetune.py``) and prints a
table of v1/v2 verdicts side-by-side. Exits 0 unless any v1=PROMOTE flips
to v2=REJECT — which is the only ruling difference that demands human
review.

Usage:
    # Defaults: gte_v8_fallback + gte_v6_2 from finetune_history.
    python scripts/sanity_v2_gate.py

    # Override snapshots (any name=path pairs):
    python scripts/sanity_v2_gate.py \\
        --snapshot v8=profiles/pay-com/finetune_history/gte_v8_fallback.json \\
        --snapshot v6=profiles/pay-com/finetune_history/gte_v6_2.json

A snapshot lacking ``file_recall_at_10`` falls into v2=HOLD with the
"unavailable" reason — that is normal, not a flip.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# scripts/ is not a package — mirror the import pattern used in tests.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.eval_verdict import verdict_from_snapshot_dual  # noqa: E402


_DEFAULT_SNAPSHOTS = (
    ("gte_v8_fallback", _REPO_ROOT / "profiles/pay-com/finetune_history/gte_v8_fallback.json"),
    ("gte_v6_2", _REPO_ROOT / "profiles/pay-com/finetune_history/gte_v6_2.json"),
)


def _load_snapshot(path: Path) -> dict:
    """Read the snapshot JSON and surface its baseline + candidate per_task dicts.

    Snapshots use ``per_task_baseline`` for the reference run and
    ``per_task_ft_v1`` for the candidate (no ``_ft`` legacy alias).
    """
    raw = json.loads(path.read_text())
    baseline = raw.get("per_task_baseline") or {}
    candidate = raw.get("per_task_ft_v1") or raw.get("per_task_ft") or {}
    if not baseline or not candidate:
        raise ValueError(
            f"snapshot {path} missing per_task_baseline / per_task_ft_v1"
        )
    return {"baseline": baseline, "candidate": candidate}


def _format_reason(reason: str, max_len: int = 60) -> str:
    if len(reason) <= max_len:
        return reason
    return reason[: max_len - 3] + "..."


def _print_table(rows: list[dict]) -> None:
    """Pretty-print a fixed-width table; columns sized to widest entry."""
    headers = ["snapshot", "v1 verdict", "v1 reason", "v2 verdict", "v2 reason"]
    widths = [
        max(len(headers[0]), max((len(r["name"]) for r in rows), default=0)),
        max(len(headers[1]), max((len(r["v1_verdict"]) for r in rows), default=0)),
        max(len(headers[2]), max((len(r["v1_reason"]) for r in rows), default=0)),
        max(len(headers[3]), max((len(r["v2_verdict"]) for r in rows), default=0)),
        max(len(headers[4]), max((len(r["v2_reason"]) for r in rows), default=0)),
    ]
    fmt = " | ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(" | ".join("-" * w for w in widths))
    for r in rows:
        print(
            fmt.format(
                r["name"],
                r["v1_verdict"],
                r["v1_reason"],
                r["v2_verdict"],
                r["v2_reason"],
            )
        )


def run(snapshot_specs: list[tuple[str, Path]]) -> int:
    """Score each snapshot under v1 + v2; return exit code (0 ok, 1 flip)."""
    rows: list[dict] = []
    flips: list[str] = []
    for name, path in snapshot_specs:
        if not path.exists():
            print(f"[warn] snapshot not found, skipping: {path}", file=sys.stderr)
            continue
        try:
            snap = _load_snapshot(path)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"[warn] could not load {path}: {exc}", file=sys.stderr)
            continue
        result = verdict_from_snapshot_dual(snap["baseline"], snap["candidate"])
        v1 = result["verdict_v1"]
        v2 = result["verdict_v2"]
        rows.append(
            {
                "name": name,
                "v1_verdict": v1["verdict"],
                "v1_reason": _format_reason(v1.get("reason", "")),
                "v2_verdict": v2["verdict"],
                "v2_reason": _format_reason(v2.get("reason", "")),
            }
        )
        # Only the v1=PROMOTE→v2=REJECT case is worth halting on. v1=PROMOTE
        # → v2=HOLD ("unavailable: legacy") is the documented behaviour.
        if v1["verdict"] == "PROMOTE" and v2["verdict"] == "REJECT":
            flips.append(name)

    if not rows:
        print("[error] no snapshots scored", file=sys.stderr)
        return 2

    _print_table(rows)
    print()
    if flips:
        print(
            f"[FAIL] {len(flips)} snapshot(s) flipped v1=PROMOTE → v2=REJECT: "
            f"{', '.join(flips)}",
            file=sys.stderr,
        )
        return 1
    print("[ok] no v1=PROMOTE → v2=REJECT flips")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--snapshot",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Add a snapshot to score (e.g. v8=profiles/pay-com/.../gte_v8.json). "
        "May be passed multiple times. If omitted, the defaults "
        "(gte_v8_fallback + gte_v6_2) are used.",
    )
    args = ap.parse_args()

    snapshot_specs: list[tuple[str, Path]]
    if args.snapshot:
        snapshot_specs = []
        for spec in args.snapshot:
            if "=" not in spec:
                ap.error(f"--snapshot needs NAME=PATH, got {spec!r}")
            name, raw_path = spec.split("=", 1)
            snapshot_specs.append((name.strip(), Path(raw_path.strip())))
    else:
        snapshot_specs = [(name, Path(p)) for name, p in _DEFAULT_SNAPSHOTS]

    return run(snapshot_specs)


if __name__ == "__main__":
    raise SystemExit(main())
