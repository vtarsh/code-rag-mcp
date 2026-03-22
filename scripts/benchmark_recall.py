#!/usr/bin/env python3
"""Benchmark recall for analyze_task across all task types.

Usage:
    python3 scripts/benchmark_recall.py                    # all tasks
    python3 scripts/benchmark_recall.py --group=CORE       # only CORE
    python3 scripts/benchmark_recall.py --group=PI,BO      # PI + BO
    python3 scripts/benchmark_recall.py --task=CORE-2586   # single task (verbose)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".pay-knowledge")))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"

# Patch out GitHub API to avoid timeouts during benchmarking
sys.path.insert(0, str(_BASE_DIR))
import src.tools.analyze.shared_sections as ss  # noqa: E402

ss.find_task_branches = lambda *a, **k: {}
ss.find_task_prs = lambda *a, **k: {}

from src.container import get_db  # noqa: E402
from src.tools.analyze import _analyze_task_impl  # noqa: E402

_BOLD_REPO_RE = re.compile(r"\*\*([a-z][a-z0-9-]+)\*\*")
_EXCLUDE_PREFIXES = ("todo", "ok", "done", "in-progress", "check", "found")


def extract_found_repos(result: str) -> set[str]:
    """Extract bold repo names from analyze_task output."""
    return {m.group(1) for m in _BOLD_REPO_RE.finditer(result) if not m.group(1).startswith(_EXCLUDE_PREFIXES)}


def run_benchmark(groups: list[str] | None = None, single_task: str | None = None) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    if single_task:
        condition = "ticket_id = ?"
        params: tuple = (single_task,)
    elif groups:
        conditions = " OR ".join(f"ticket_id LIKE '{g}-%'" for g in groups)
        condition = f"({conditions})"
        params = ()
    else:
        condition = "1=1"
        params = ()

    rows = conn.execute(
        f"SELECT ticket_id, summary, repos_changed FROM task_history WHERE {condition} ORDER BY ticket_id",
        params,
    ).fetchall()

    group_stats: dict[str, list[int]] = {}
    total_hits, total_actual = 0, 0

    for r in rows:
        tid = r["ticket_id"]
        actual = set(json.loads(r["repos_changed"]) if r["repos_changed"] else [])
        if not actual:
            continue

        db = get_db()
        try:
            result = _analyze_task_impl(db, r["summary"] + " " + tid, "")
        finally:
            db.close()

        found = extract_found_repos(result)
        hits = actual & found
        recall = len(hits) / len(actual) * 100
        missed = sorted(actual - found)

        prefix = tid.split("-")[0]
        group_stats.setdefault(prefix, [0, 0])
        group_stats[prefix][0] += len(hits)
        group_stats[prefix][1] += len(actual)
        total_hits += len(hits)
        total_actual += len(actual)

        if single_task or recall < 50:
            print(f"{tid:12s} {recall:5.0f}% ({len(hits):2d}/{len(actual):2d}) missed={missed[:5]}")
        elif recall < 100:
            print(f"{tid:12s} {recall:5.0f}% ({len(hits):2d}/{len(actual):2d})")

    print("\n" + "=" * 50)
    for g, (h, a) in sorted(group_stats.items()):
        print(f"{g:6s} {h / a * 100:.1f}% ({h}/{a})")
    if total_actual:
        print(f"{'TOTAL':6s} {total_hits / total_actual * 100:.1f}% ({total_hits}/{total_actual})")
    print("=" * 50)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark analyze_task recall")
    parser.add_argument("--group", help="Comma-separated groups: CORE,PI,BO,HS")
    parser.add_argument("--task", help="Single task ID for verbose output")
    args = parser.parse_args()

    groups = [g.strip().upper() for g in args.group.split(",")] if args.group else None
    run_benchmark(groups=groups, single_task=args.task)


if __name__ == "__main__":
    main()
