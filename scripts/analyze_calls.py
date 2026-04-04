#!/usr/bin/env python3
"""Analyze MCP tool call logs — usage patterns, frequency, timing.

Usage:
    python scripts/analyze_calls.py              # full summary
    python scripts/analyze_calls.py --last 20    # last 20 calls
    python scripts/analyze_calls.py --sessions   # per-session breakdown
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "logs" / "mcp_calls.jsonl"


def load_calls() -> list[dict]:
    if not LOG_PATH.exists():
        print(f"No log file at {LOG_PATH}")
        return []
    calls = []
    for line in LOG_PATH.read_text().splitlines():
        if line.strip():
            try:
                calls.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return calls


def summary(calls: list[dict]) -> None:
    if not calls:
        print("No calls recorded yet.")
        return

    print(f"Total calls: {len(calls)}")
    print(f"Sessions: {len(set(c['session'] for c in calls))}")
    print(f"Period: {calls[0]['ts'][:10]} — {calls[-1]['ts'][:10]}")
    print()

    # Tool frequency
    tool_counts = Counter(c["tool"] for c in calls)
    print("## Tool Usage (most → least)")
    for tool, count in tool_counts.most_common():
        durations = [c["duration_ms"] for c in calls if c["tool"] == tool]
        avg_ms = sum(durations) / len(durations)
        print(f"  {tool:25s}  {count:4d} calls  avg {avg_ms:6.0f}ms")
    print()

    # Never used tools (from known set)
    all_tools = {
        "search", "find_dependencies", "trace_impact", "trace_flow",
        "trace_chain", "trace_field", "repo_overview", "list_repos",
        "analyze_task", "context_builder", "health_check", "visualize_graph",
        "diff_provider_config", "search_task_history",
    }
    never_used = all_tools - set(tool_counts.keys())
    if never_used:
        print(f"## Never Used: {', '.join(sorted(never_used))}")
        print()

    # Common query patterns (from search/analyze_task args)
    queries = []
    for c in calls:
        q = c["args"].get("query") or c["args"].get("description") or ""
        if q:
            queries.append((c["tool"], q[:80]))
    if queries:
        print(f"## Recent Queries (last 10)")
        for tool, q in queries[-10:]:
            print(f"  [{tool}] {q}")


def show_last(calls: list[dict], n: int) -> None:
    for c in calls[-n:]:
        tool = c["tool"]
        args_short = {k: (v[:60] + "..." if isinstance(v, str) and len(v) > 60 else v) for k, v in c["args"].items()}
        print(f"{c['ts'][:19]}  {tool:20s}  {c['duration_ms']:5.0f}ms  {c['result_len']:6d}ch  {args_short}")


def show_sessions(calls: list[dict]) -> None:
    sessions: dict[str, list[dict]] = {}
    for c in calls:
        sessions.setdefault(c["session"], []).append(c)

    for sid, session_calls in sessions.items():
        tools = Counter(c["tool"] for c in session_calls)
        top = ", ".join(f"{t}({n})" for t, n in tools.most_common(5))
        print(f"Session {sid[:12]}…  {len(session_calls)} calls  [{session_calls[0]['ts'][:16]}]  {top}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze MCP call logs")
    parser.add_argument("--last", type=int, help="Show last N calls")
    parser.add_argument("--sessions", action="store_true", help="Per-session breakdown")
    args = parser.parse_args()

    calls = load_calls()
    if not calls:
        return

    if args.last:
        show_last(calls, args.last)
    elif args.sessions:
        show_sessions(calls)
    else:
        summary(calls)


if __name__ == "__main__":
    main()
