#!/usr/bin/env python3
"""Analyze search feedback logs for ranking quality insights.

Reads ~/.code-rag/logs/search_feedback.jsonl and produces:
  - Query frequency and result distributions
  - Most/least returned repos (potential false positives)
  - Queries with 0 results (coverage gaps)
  - Score distributions for tuning thresholds
  - Re-search patterns (same query within short window = poor results)

Usage:
    python3 scripts/analyze_feedback.py
    python3 scripts/analyze_feedback.py --days=7
    python3 scripts/analyze_feedback.py --top=20
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
LOG_FILE = _BASE / "logs" / "search_feedback.jsonl"


def load_entries(days: int = 30) -> list[dict]:
    if not LOG_FILE.exists():
        print(f"No feedback log found at {LOG_FILE}")
        return []

    cutoff = datetime.now() - timedelta(days=days)
    entries = []
    for line in LOG_FILE.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            ts = datetime.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%S")
            if ts >= cutoff:
                entries.append(entry)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return entries


def analyze(entries: list[dict], top: int = 15) -> None:
    if not entries:
        print("No entries to analyze.")
        return

    print(f"{'=' * 60}")
    print(f"Search Feedback Analysis ({len(entries)} entries)")
    print(f"{'=' * 60}")

    # --- Tool distribution ---
    tool_counts = Counter(e["tool"] for e in entries)
    print("\nTool usage:")
    for tool, cnt in tool_counts.most_common():
        print(f"  {tool}: {cnt}")

    # --- Queries with 0 results ---
    zero_results = [e for e in entries if e["result_count"] == 0]
    if zero_results:
        print(f"\nQueries with 0 results ({len(zero_results)}):")
        for e in zero_results[:top]:
            print(f"  [{e['tool']}] {e['query']}")

    # --- Most frequent queries ---
    query_counts = Counter(e["query"] for e in entries)
    print(f"\nTop {top} queries:")
    for q, cnt in query_counts.most_common(top):
        print(f"  {cnt:>3}x  {q[:60]}")

    # --- Most returned repos (potential false positives if too frequent) ---
    repo_counts = Counter()
    for e in entries:
        for r in e["results"]:
            repo_counts[r["repo"]] += 1
    print(f"\nTop {top} repos in results:")
    for repo, cnt in repo_counts.most_common(top):
        print(f"  {cnt:>4}x  {repo}")

    # --- Score distribution ---
    all_scores = []
    for e in entries:
        for r in e["results"]:
            if r["score"] > 0:
                all_scores.append(r["score"])
    if all_scores:
        all_scores.sort()
        n = len(all_scores)
        print(f"\nScore distribution ({n} results with score > 0):")
        print(f"  Min:    {all_scores[0]:.4f}")
        print(f"  P25:    {all_scores[n // 4]:.4f}")
        print(f"  Median: {all_scores[n // 2]:.4f}")
        print(f"  P75:    {all_scores[3 * n // 4]:.4f}")
        print(f"  Max:    {all_scores[-1]:.4f}")

    # --- Re-search detection (same user re-queries within 2 min) ---
    re_searches = []
    sorted_entries = sorted(entries, key=lambda e: e["ts"])
    for i in range(1, len(sorted_entries)):
        prev, curr = sorted_entries[i - 1], sorted_entries[i]
        try:
            t_prev = datetime.strptime(prev["ts"], "%Y-%m-%dT%H:%M:%S")
            t_curr = datetime.strptime(curr["ts"], "%Y-%m-%dT%H:%M:%S")
            if (t_curr - t_prev).total_seconds() < 120 and prev["query"] != curr["query"]:
                # Different query within 2 min = possible re-search
                re_searches.append((prev["query"], curr["query"]))
        except (ValueError, KeyError):
            continue

    if re_searches:
        print(f"\nPossible re-searches ({len(re_searches)} within 2-min window):")
        for q1, q2 in re_searches[:top]:
            print(f"  '{q1[:40]}' → '{q2[:40]}'")

    # --- Candidates-to-results ratio ---
    ratios = []
    for e in entries:
        if e.get("total_candidates", 0) > 0:
            ratios.append(e["result_count"] / e["total_candidates"])
    if ratios:
        avg_ratio = sum(ratios) / len(ratios)
        print(f"\nAvg results/candidates ratio: {avg_ratio:.2%}")

    print(f"\n{'=' * 60}")


def main() -> None:
    days = 30
    top = 15
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
        elif arg.startswith("--top="):
            top = int(arg.split("=")[1])

    entries = load_entries(days)
    analyze(entries, top)


if __name__ == "__main__":
    main()
