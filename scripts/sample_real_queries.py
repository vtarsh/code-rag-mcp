#!/usr/bin/env python3
"""Sample real MCP search queries from tool_calls.jsonl for real-query eval.

Input: logs/tool_calls.jsonl — captured MCP tool invocations (one JSON per line).
Output: profiles/{profile}/real_queries/sampled.jsonl — one query per line with
        session metadata for downstream labeling.

Stratification rules:
  1. Tool filter = 'search' (the tool whose relevance metric applies).
  2. Normalise whitespace + lowercase for dedup key, preserve original for display.
  3. Session split by IDLE_GAP between consecutive timestamps (default 30 min).
  4. Per-session cap (default 5) to break self-replay bias — the top-3 sessions
     account for ~47% of the volume (single-dev workflow replay).
  5. Global dedup: a query that recurs across sessions is kept once.
  6. Final shuffle with a seed, take top N.

No external deps — stdlib only.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_IDLE_GAP_MIN = 30
# cap=15 selected to hit N=400 unique queries without over-diluting
# session variety. With 85 sessions, cap=5 yields only 234 unique
# (too many duplicates within sessions); cap=15 produces 509 pre-sample,
# post-shuffle trimmed to N. Top-3 sessions contribute <= 15 each = ~11%
# of a 400-sample, well below the uncapped 47% dominance.
DEFAULT_PER_SESSION_CAP = 15
DEFAULT_SAMPLE_SIZE = 400
DEFAULT_SEED = 42

_WS = re.compile(r"\s+")


def _normalize(q: str) -> str:
    return _WS.sub(" ", q or "").strip().lower()


def _parse_ts(s: str) -> datetime:
    # Accept ISO-8601 with or without fractional seconds / timezone.
    return datetime.fromisoformat(s)


def load_search_queries(path: Path) -> list[dict]:
    """Return list of {ts, query, norm} sorted by ts ascending."""
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("tool") != "search":
                continue
            q = ((e.get("args") or {}).get("query") or "").strip()
            if not q:
                continue
            try:
                ts = _parse_ts(e["ts"])
            except (KeyError, ValueError):
                continue
            out.append({"ts": ts, "query": q, "norm": _normalize(q), "lineno": lineno})
    out.sort(key=lambda x: x["ts"])
    return out


def split_sessions(entries: list[dict], idle_gap: timedelta) -> list[list[dict]]:
    if not entries:
        return []
    sessions: list[list[dict]] = [[entries[0]]]
    for e in entries[1:]:
        prev_ts = sessions[-1][-1]["ts"]
        if e["ts"] - prev_ts > idle_gap:
            sessions.append([e])
        else:
            sessions[-1].append(e)
    return sessions


def sample_stratified(
    entries: list[dict],
    *,
    n: int,
    per_session_cap: int,
    idle_gap: timedelta,
    seed: int,
) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    sessions = split_sessions(entries, idle_gap)

    # Per-session dedup + cap.
    pool: list[dict] = []
    for session in sessions:
        seen_here: set[str] = set()
        kept: list[dict] = []
        for e in session:
            if e["norm"] in seen_here:
                continue
            seen_here.add(e["norm"])
            kept.append(e)
            if len(kept) >= per_session_cap:
                break
        pool.extend(kept)

    # Global dedup — query might recur across sessions (e.g. "search X" in two
    # unrelated workflows). Keep first occurrence to preserve temporal anchor.
    seen_global: set[str] = set()
    deduped: list[dict] = []
    for e in pool:
        if e["norm"] in seen_global:
            continue
        seen_global.add(e["norm"])
        deduped.append(e)

    rng.shuffle(deduped)
    sampled = deduped[:n]

    stats = {
        "total_search_calls": len(entries),
        "total_sessions": len(sessions),
        "after_per_session_cap": len(pool),
        "after_global_dedup": len(deduped),
        "sampled": len(sampled),
        "per_session_cap": per_session_cap,
        "idle_gap_min": int(idle_gap.total_seconds() // 60),
        "seed": seed,
    }
    return sampled, stats


def write_output(path: Path, sampled: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in sampled:
            rec = {
                "query": e["query"],
                "sampled_ts": e["ts"].isoformat(),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--input",
        type=Path,
        default=Path("logs/tool_calls.jsonl"),
        help="Path to tool_calls.jsonl (default: logs/tool_calls.jsonl)",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("profiles/pay-com/real_queries/sampled.jsonl"),
        help="Output sampled queries (default: profiles/pay-com/real_queries/sampled.jsonl)",
    )
    p.add_argument("-n", "--count", type=int, default=DEFAULT_SAMPLE_SIZE, help=f"Sample size (default {DEFAULT_SAMPLE_SIZE})")
    p.add_argument(
        "--per-session-cap",
        type=int,
        default=DEFAULT_PER_SESSION_CAP,
        help=f"Max queries kept per session (default {DEFAULT_PER_SESSION_CAP})",
    )
    p.add_argument(
        "--idle-gap-min",
        type=int,
        default=DEFAULT_IDLE_GAP_MIN,
        help=f"Session idle gap in minutes (default {DEFAULT_IDLE_GAP_MIN})",
    )
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = p.parse_args()

    if not args.input.exists():
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 1
    if args.count <= 0:
        print(f"ERROR: --count must be positive, got {args.count}", file=sys.stderr)
        return 1

    entries = load_search_queries(args.input)
    if not entries:
        print(f"ERROR: no search entries in {args.input}", file=sys.stderr)
        return 1

    sampled, stats = sample_stratified(
        entries,
        n=args.count,
        per_session_cap=args.per_session_cap,
        idle_gap=timedelta(minutes=args.idle_gap_min),
        seed=args.seed,
    )

    write_output(args.output, sampled)

    for k, v in stats.items():
        print(f"{k}: {v}")
    print(f"output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
