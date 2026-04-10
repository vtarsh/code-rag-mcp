#!/usr/bin/env python3
"""Analyze session quality from tool_calls.jsonl.

Per session: total calls, unique queries, repeat queries (3+),
fallback chains (trace→search), thrashing index.

Usage:
    python scripts/analyze_session_quality.py
    python scripts/analyze_session_quality.py --last 5   # last N sessions
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "tool_calls.jsonl"

# Session gap: 30 minutes without a call = new session
SESSION_GAP = timedelta(minutes=30)


def load_calls() -> list[dict]:
    """Load all tool call entries."""
    if not LOG_PATH.exists():
        print(f"No log file at {LOG_PATH}")
        sys.exit(1)

    calls = []
    for line in LOG_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return calls


def split_sessions(calls: list[dict]) -> list[list[dict]]:
    """Split calls into sessions based on time gaps."""
    if not calls:
        return []

    # Sort by timestamp
    calls.sort(key=lambda c: c.get("ts", ""))
    sessions: list[list[dict]] = [[calls[0]]]

    for call in calls[1:]:
        prev_ts = sessions[-1][-1].get("ts", "")
        curr_ts = call.get("ts", "")
        try:
            prev_dt = datetime.fromisoformat(prev_ts)
            curr_dt = datetime.fromisoformat(curr_ts)
            if curr_dt - prev_dt > SESSION_GAP:
                sessions.append([])
        except (ValueError, TypeError):
            pass
        sessions[-1].append(call)

    return sessions


def analyze_session(calls: list[dict]) -> dict:
    """Analyze a single session."""
    total = len(calls)
    tool_counts = Counter(c.get("tool", "unknown") for c in calls)

    # Extract search queries
    queries = []
    for c in calls:
        args = c.get("args", {})
        if isinstance(args, dict):
            q = args.get("query", "") or args.get("description", "")
            if q:
                queries.append(q.lower().strip())

    unique_queries = len(set(queries))
    query_counts = Counter(queries)
    repeats = {q: cnt for q, cnt in query_counts.items() if cnt >= 3}

    # Detect fallback chains: trace_field/trace_flow/trace_chain followed by search
    fallback_chains = 0
    trace_tools = {"trace_field", "trace_flow", "trace_chain", "trace_impact"}
    for i in range(len(calls) - 1):
        if calls[i].get("tool") in trace_tools and calls[i + 1].get("tool") == "search":
            fallback_chains += 1

    # Thrashing index: ratio of repeat queries to total queries
    repeat_count = sum(cnt - 1 for cnt in query_counts.values() if cnt > 1)
    thrashing = repeat_count / total if total > 0 else 0

    # Duration
    durations = [c.get("duration_ms", 0) for c in calls if c.get("duration_ms")]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Time range
    first_ts = calls[0].get("ts", "?")[:19]
    last_ts = calls[-1].get("ts", "?")[:19]

    return {
        "time_range": f"{first_ts} → {last_ts}",
        "total_calls": total,
        "tool_distribution": dict(tool_counts.most_common()),
        "unique_queries": unique_queries,
        "repeat_queries_3plus": repeats,
        "fallback_chains": fallback_chains,
        "thrashing_index": round(thrashing, 3),
        "avg_duration_ms": round(avg_duration),
    }


def main():
    last_n = None
    if "--last" in sys.argv:
        idx = sys.argv.index("--last")
        if idx + 1 < len(sys.argv):
            last_n = int(sys.argv[idx + 1])

    calls = load_calls()
    sessions = split_sessions(calls)

    if last_n:
        sessions = sessions[-last_n:]

    print(f"{'='*70}")
    print(f"Session Quality Analysis — {len(sessions)} sessions")
    print(f"{'='*70}")

    for i, session in enumerate(sessions, 1):
        stats = analyze_session(session)
        print(f"\n--- Session {i}: {stats['time_range']} ---")
        print(f"  Calls: {stats['total_calls']}  |  Unique queries: {stats['unique_queries']}  |  Avg duration: {stats['avg_duration_ms']}ms")
        print(f"  Thrashing index: {stats['thrashing_index']}  |  Fallback chains: {stats['fallback_chains']}")

        tools = stats["tool_distribution"]
        tool_str = ", ".join(f"{t}:{n}" for t, n in tools.items())
        print(f"  Tools: {tool_str}")

        if stats["repeat_queries_3plus"]:
            print(f"  Repeat queries (3+):")
            for q, cnt in stats["repeat_queries_3plus"].items():
                print(f"    [{cnt}x] {q[:80]}")

    # Summary table
    print(f"\n{'='*70}")
    print(f"{'Session':<10} {'Calls':<8} {'Unique Q':<10} {'Thrash':<10} {'Fallback':<10}")
    print(f"{'-'*48}")
    for i, session in enumerate(sessions, 1):
        stats = analyze_session(session)
        print(f"{'S'+str(i):<10} {stats['total_calls']:<8} {stats['unique_queries']:<10} {stats['thrashing_index']:<10} {stats['fallback_chains']:<10}")


if __name__ == "__main__":
    main()
