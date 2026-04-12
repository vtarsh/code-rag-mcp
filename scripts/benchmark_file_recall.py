#!/usr/bin/env python3
"""File-level recall benchmark for analyze_task.

Measures what % of actually-changed files are in repos predicted by analyze_task.
This is finer-grained than repo-level recall: a repo with 40 changed files weighs
more than one with 1 file.

Usage:
    python3 scripts/benchmark_file_recall.py                    # all tasks
    python3 scripts/benchmark_file_recall.py --group=PI         # only PI
    python3 scripts/benchmark_file_recall.py --task=PI-60       # single task (verbose)
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

sys.path.insert(0, str(_BASE_DIR))
import src.tools.analyze.shared_sections as ss  # noqa: E402

ss.find_task_branches = lambda *a, **k: {}
ss.find_task_prs = lambda *a, **k: {}

from src.container import get_db  # noqa: E402
from src.tools.analyze import _analyze_task_impl  # noqa: E402

_BOLD_REPO_RE = re.compile(r"\*\*([a-z][a-z0-9-]+)\*\*")
_EXCLUDE_PREFIXES = ("todo", "ok", "done", "in-progress", "check", "found")

BOILERPLATE_PATTERNS = (
    ".github/workflows/",
    "package-lock.json",
    "yarn.lock",
    ".eslintrc",
    ".prettierrc",
    "Dockerfile",
    ".dockerignore",
)

BOILERPLATE_REPOS = (
    "boilerplate-node-providers-grpc-service",
)


def _is_boilerplate(filepath: str) -> bool:
    if any(p in filepath for p in BOILERPLATE_PATTERNS):
        return True
    repo = filepath.split("/")[0] if "/" in filepath else filepath
    return repo in BOILERPLATE_REPOS


def _parse_files(files_changed: str | None) -> list[str]:
    """Parse files_changed JSON, filter boilerplate."""
    if not files_changed:
        return []
    files = json.loads(files_changed)
    return [f for f in files if not _is_boilerplate(f)]


def _file_repo(filepath: str) -> str | None:
    """Extract repo name from 'repo/path/to/file' format."""
    if "/" in filepath:
        return filepath.split("/")[0]
    return None


def _extract_predicted_repos(result: str) -> set[str]:
    return {
        m.group(1)
        for m in _BOLD_REPO_RE.finditer(result)
        if not m.group(1).startswith(_EXCLUDE_PREFIXES)
    }


def run_benchmark(
    groups: list[str] | None = None,
    single_task: str | None = None,
) -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
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
        f"SELECT ticket_id, summary, description, files_changed FROM task_history WHERE {condition} ORDER BY ticket_id",
        params,
    ).fetchall()

    total_files_hit = 0
    total_files_actual = 0
    total_files_extra = 0
    group_stats: dict[str, list[int]] = {}  # [hit, actual, extra]

    for r in rows:
        tid = r["ticket_id"]
        files = _parse_files(r["files_changed"])
        if not files:
            continue

        # Build ground truth: file -> repo
        file_repos: dict[str, str] = {}
        for f in files:
            repo = _file_repo(f)
            if repo:
                file_repos[f] = repo
        if not file_repos:
            continue

        actual_repos = set(file_repos.values())

        db = get_db()
        try:
            desc = r["summary"] + " " + tid
            task_desc = r["description"] or ""
            if task_desc:
                desc += " " + task_desc[:300]
            result = _analyze_task_impl(db, desc, "", rerank=False)
        finally:
            db.close()

        predicted = _extract_predicted_repos(result)

        # File-level metrics
        files_hit = sum(1 for repo in file_repos.values() if repo in predicted)
        files_total = len(file_repos)
        recall = files_hit / files_total * 100

        # Extra predicted repos (not in ground truth)
        extra_repos = predicted - actual_repos
        extra_count = len(extra_repos)

        prefix = tid.split("-")[0]
        group_stats.setdefault(prefix, [0, 0, 0])
        group_stats[prefix][0] += files_hit
        group_stats[prefix][1] += files_total
        group_stats[prefix][2] += extra_count
        total_files_hit += files_hit
        total_files_actual += files_total
        total_files_extra += extra_count

        # Print per-task details
        missed_repos = actual_repos - predicted
        missed_files = [f for f, repo in file_repos.items() if repo not in predicted]

        if single_task:
            print(f"{tid:12s} file_recall={recall:5.1f}% ({files_hit}/{files_total} files)")
            print(f"{'':12s} actual_repos={sorted(actual_repos)}")
            print(f"{'':12s} predicted_repos={sorted(predicted & actual_repos)}")
            if missed_repos:
                print(f"{'':12s} missed_repos={sorted(missed_repos)}")
            if extra_repos:
                print(f"{'':12s} extra_repos={sorted(extra_repos)}")
            if missed_files:
                print(f"{'':12s} missed_files ({len(missed_files)}):")
                for f in sorted(missed_files)[:15]:
                    print(f"{'':14s} {f}")
        elif recall < 100:
            print(
                f"{tid:12s} {recall:5.1f}% ({files_hit:3d}/{files_total:3d} files)"
                f"  missed_repos={sorted(missed_repos)[:3]}"
            )

    # Summary
    print("\n" + "=" * 70)
    for g, (h, a, e) in sorted(group_stats.items()):
        g_recall = h / a * 100 if a else 0
        print(f"{g:6s} file_recall={g_recall:5.1f}% ({h}/{a} files)  extra_repos={e}")
    if total_files_actual:
        t_recall = total_files_hit / total_files_actual * 100
        print(
            f"{'TOTAL':6s} file_recall={t_recall:5.1f}%"
            f" ({total_files_hit}/{total_files_actual} files)"
            f"  extra_repos={total_files_extra}"
        )
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="File-level recall benchmark")
    parser.add_argument("--group", help="Comma-separated groups: CORE,PI,BO,HS")
    parser.add_argument("--task", help="Single task ID for verbose output")
    args = parser.parse_args()

    groups = [g.strip().upper() for g in args.group.split(",")] if args.group else None
    run_benchmark(groups=groups, single_task=args.task)


if __name__ == "__main__":
    main()
