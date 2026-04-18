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

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", str(Path.home() / ".code-rag-mcp")))
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


def _repos_with_files(files_changed: str | None, repos: set[str]) -> set[str]:
    """Filter repos to only those that have actual file changes."""
    if not files_changed:
        return repos  # no files data — keep all repos (don't penalize)
    files = json.loads(files_changed)
    if not files:
        return repos
    repos_with = set()
    for f in files:
        if "/" in f:
            repos_with.add(f.split("/")[0])
    # Only filter if we found at least 1 repo in files — otherwise data might be missing
    return (repos & repos_with) if repos_with else repos


_PKG_BUMP_FILES = {"package.json", "package-lock.json", "yarn.lock"}


def _filter_pkg_bump_repos(files_changed: str | None, repos: set[str]) -> tuple[set[str], int]:
    """Exclude repos whose only changed files are package bump artifacts.

    Returns (filtered_repos, count_of_excluded_repos).
    """
    if not files_changed:
        return repos, 0  # no files data — keep all repos
    files = json.loads(files_changed)
    if not files:
        return repos, 0

    # Build mapping: repo -> set of file basenames
    repo_files: dict[str, set[str]] = {}
    for f in files:
        if "/" in f:
            repo = f.split("/")[0]
            filename = f.rsplit("/", 1)[-1]
            repo_files.setdefault(repo, set()).add(filename)

    pkg_only_repos = set()
    for repo, filenames in repo_files.items():
        if filenames and filenames <= _PKG_BUMP_FILES:
            pkg_only_repos.add(repo)

    filtered = repos - pkg_only_repos
    return filtered, len(repos) - len(filtered)


def run_benchmark(
    groups: list[str] | None = None,
    single_task: str | None = None,
    filter_phantoms: bool = False,
    filter_pkg_bumps: bool = False,
) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    # Attach tasks.db for task_history (lives in separate DB since arch fix).
    _tasks_db = DB_PATH.parent / "tasks.db"
    if _tasks_db.exists():
        conn.execute(f"ATTACH DATABASE '{_tasks_db}' AS tasks")

    if single_task:
        condition = "ticket_id = ?"
        params: tuple = (single_task,)
    elif groups:
        conditions = " OR ".join("ticket_id LIKE ?" for _ in groups)
        condition = f"({conditions})"
        params = tuple(f"{g}-%" for g in groups)
    else:
        condition = "1=1"
        params = ()

    rows = conn.execute(
        f"SELECT ticket_id, summary, repos_changed, files_changed, description FROM task_history WHERE {condition} ORDER BY ticket_id",
        params,
    ).fetchall()

    # [hits, actual, found_total] per group
    group_stats: dict[str, list[int]] = {}
    total_hits, total_actual, total_found = 0, 0, 0
    phantom_count = 0
    pkg_bump_count = 0

    for r in rows:
        tid = r["ticket_id"]
        actual = set(json.loads(r["repos_changed"]) if r["repos_changed"] else [])
        if not actual:
            continue

        if filter_phantoms:
            filtered = _repos_with_files(r["files_changed"], actual)
            phantom_count += len(actual) - len(filtered)
            actual = filtered
            if not actual:
                continue

        if filter_pkg_bumps:
            actual, excluded = _filter_pkg_bump_repos(r["files_changed"], actual)
            pkg_bump_count += excluded
            if not actual:
                continue

        db = get_db()
        try:
            desc = r["summary"] + " " + tid
            # Include first 300 chars of description for richer context
            task_desc = r["description"] or ""
            if task_desc:
                desc += " " + task_desc[:300]
            result = _analyze_task_impl(db, desc, "")
        finally:
            db.close()

        found = extract_found_repos(result)
        hits = actual & found
        recall = len(hits) / len(actual) * 100
        precision = len(hits) / len(found) * 100 if found else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        missed = sorted(actual - found)

        prefix = tid.split("-")[0]
        group_stats.setdefault(prefix, [0, 0, 0])
        group_stats[prefix][0] += len(hits)
        group_stats[prefix][1] += len(actual)
        group_stats[prefix][2] += len(found)
        total_hits += len(hits)
        total_actual += len(actual)
        total_found += len(found)

        if single_task:
            print(
                f"{tid:12s} recall={recall:5.1f}% prec={precision:5.1f}% F1={f1:5.1f}%"
                f" ({len(hits)}/{len(actual)} expected, {len(found)} predicted)"
            )
            if missed:
                print(f"{'':12s} missed={missed[:5]}")
        elif recall < 50:
            print(f"{tid:12s} {recall:5.0f}% ({len(hits):2d}/{len(actual):2d}) missed={missed[:5]}")
        elif recall < 100:
            print(f"{tid:12s} {recall:5.0f}% ({len(hits):2d}/{len(actual):2d})")

    print("\n" + "=" * 70)
    if filter_phantoms:
        print(f"[phantom filter ON: {phantom_count} phantom repos excluded]")
    if filter_pkg_bumps:
        print(f"[pkg-bump filter ON: {pkg_bump_count} pkg-bump-only repos excluded]")
    for g, (h, a, f) in sorted(group_stats.items()):
        g_recall = h / a * 100
        g_precision = h / f * 100 if f else 0.0
        g_f1 = 2 * g_precision * g_recall / (g_precision + g_recall) if (g_precision + g_recall) else 0.0
        print(
            f"{g:6s} {g_recall:5.1f}% recall, {g_precision:5.1f}% precision, F1={g_f1:5.1f}% ({h}/{a} found, {h}/{f} predicted)"
        )
    if total_actual:
        t_recall = total_hits / total_actual * 100
        t_precision = total_hits / total_found * 100 if total_found else 0.0
        t_f1 = 2 * t_precision * t_recall / (t_precision + t_recall) if (t_precision + t_recall) else 0.0
        print(
            f"{'TOTAL':6s} {t_recall:5.1f}% recall, {t_precision:5.1f}% precision, F1={t_f1:5.1f}% ({total_hits}/{total_actual} found, {total_hits}/{total_found} predicted)"
        )
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark analyze_task recall")
    parser.add_argument("--group", help="Comma-separated groups: CORE,PI,BO,HS")
    parser.add_argument("--task", help="Single task ID for verbose output")
    parser.add_argument(
        "--filter-phantoms", action="store_true", help="Exclude repos with zero files_changed from ground truth"
    )
    parser.add_argument(
        "--filter-pkg-bumps",
        action="store_true",
        default=True,
        help="Exclude repos whose only changes are package.json/package-lock.json/yarn.lock (default: on)",
    )
    parser.add_argument(
        "--no-filter-pkg-bumps",
        action="store_true",
        help="Include pkg-bump-only repos in ground truth",
    )
    args = parser.parse_args()

    groups = [g.strip().upper() for g in args.group.split(",")] if args.group else None
    run_benchmark(
        groups=groups,
        single_task=args.task,
        filter_phantoms=args.filter_phantoms,
        filter_pkg_bumps=not args.no_filter_pkg_bumps,
    )


if __name__ == "__main__":
    main()
