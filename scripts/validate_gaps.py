#!/usr/bin/env python3
"""
Phase 9: Validate task_gaps using existing PR data and status_changelog.

For each HIGH-confidence gap, checks:
1. Does the task's pr_urls already contain a PR for the expected repo?
   → If yes: mark as "unlinked_pr" (repo WAS changed, just not in repos_changed)
2. Does another task in the same time window touch the expected repo?
   → If yes: mark as "related_work" (likely handled in a separate task)
3. Otherwise: mark as "potential_miss" (needs human review)

Usage:
    python scripts/validate_gaps.py                # analyze only, print report
    python scripts/validate_gaps.py --save         # save validation results to DB
    python scripts/validate_gaps.py --confidence 0.5  # lower confidence threshold
"""

import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"


def extract_repo_from_pr_url(url: str) -> str:
    """Extract repo name from GitHub PR URL."""
    m = re.search(r"github\.com/[^/]+/([^/]+)/pull/", url)
    return m.group(1) if m else ""


def get_task_date_range(status_changelog: str) -> tuple[datetime | None, datetime | None]:
    """Extract start/end dates from status_changelog JSON."""
    if not status_changelog:
        return None, None
    try:
        changes = json.loads(status_changelog)
        if not changes:
            return None, None
        # Array is newest-first, so last element = earliest transition
        dates = []
        for c in changes:
            date_str = c.get("date", "")
            if date_str:
                # Parse ISO date, strip timezone for simplicity
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00").split("+")[0].split("T")[0])
                dates.append(dt)
        if not dates:
            return None, None
        return min(dates), max(dates)
    except (json.JSONDecodeError, ValueError):
        return None, None


def validate():
    save = "--save" in sys.argv
    conf_threshold = 0.8
    for i, arg in enumerate(sys.argv):
        if arg == "--confidence" and i + 1 < len(sys.argv):
            conf_threshold = float(sys.argv[i + 1])

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Ensure validation column exists
    try:
        conn.execute("ALTER TABLE task_gaps ADD COLUMN validation TEXT DEFAULT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Load all tasks with their PR URLs and date ranges
    tasks = {}
    for row in conn.execute("SELECT ticket_id, pr_urls, repos_changed, status_changelog, developer FROM task_history"):
        pr_urls = json.loads(row["pr_urls"] or "[]")
        pr_repos = {extract_repo_from_pr_url(url) for url in pr_urls} - {""}
        repos_changed = set(json.loads(row["repos_changed"] or "[]"))
        start, end = get_task_date_range(row["status_changelog"])
        tasks[row["ticket_id"]] = {
            "pr_repos": pr_repos,
            "repos_changed": repos_changed,
            "all_repos": pr_repos | repos_changed,
            "start": start,
            "end": end,
            "developer": row["developer"] or "",
        }

    # Build repo → tasks index (for related_work detection)
    repo_tasks: dict[str, list[str]] = defaultdict(list)
    for tid, tdata in tasks.items():
        for repo in tdata["all_repos"]:
            repo_tasks[repo].append(tid)

    # Load HIGH confidence gaps
    gaps = conn.execute(
        "SELECT id, ticket_id, expected, confidence, gap_type, validation FROM task_gaps WHERE confidence >= ?",
        (conf_threshold,),
    ).fetchall()

    print(f"=== Gap Validation ({len(gaps)} gaps at confidence >= {conf_threshold}) ===\n")

    stats = {"unlinked_pr": 0, "related_work": 0, "potential_miss": 0, "no_data": 0}
    validations: list[tuple[str, int]] = []  # (validation_result, gap_id)

    for gap in gaps:
        tid = gap["ticket_id"]
        expected = gap["expected"]
        task = tasks.get(tid)

        if not task:
            stats["no_data"] += 1
            validations.append(("no_data", gap["id"]))
            continue

        # Check 1: PR already exists for expected repo in this task
        if expected in task["pr_repos"]:
            stats["unlinked_pr"] += 1
            validations.append(("unlinked_pr", gap["id"]))
            continue

        # Check 2: Another task by same developer touches expected repo in overlapping time
        if task["start"] and task["end"]:
            buffer_days = 3
            window_start = task["start"] - timedelta(days=buffer_days)
            window_end = task["end"] + timedelta(days=buffer_days)

            related = False
            for other_tid in repo_tasks.get(expected, []):
                if other_tid == tid:
                    continue
                other = tasks.get(other_tid)
                if not other or not other["start"] or not other["end"]:
                    continue
                # Check time overlap + same developer
                if (
                    other["start"] <= window_end
                    and other["end"] >= window_start
                    and task["developer"]
                    and task["developer"] == other["developer"]
                ):
                    related = True
                    break
            if related:
                stats["related_work"] += 1
                validations.append(("related_work", gap["id"]))
                continue

        stats["potential_miss"] += 1
        validations.append(("potential_miss", gap["id"]))

    # Report
    total = len(gaps)
    print(
        f"  unlinked_pr:    {stats['unlinked_pr']:4d} ({stats['unlinked_pr'] * 100 // max(total, 1)}%) — repo HAS PRs in task, just not in repos_changed"
    )
    print(
        f"  related_work:   {stats['related_work']:4d} ({stats['related_work'] * 100 // max(total, 1)}%) — same dev changed repo in overlapping task"
    )
    print(
        f"  potential_miss: {stats['potential_miss']:4d} ({stats['potential_miss'] * 100 // max(total, 1)}%) — no evidence found, may be real gap"
    )
    print(f"  no_data:        {stats['no_data']:4d}")
    print()

    # Top potential_miss repos
    miss_repos = defaultdict(int)
    for v, gid in validations:
        if v == "potential_miss":
            gap = next(g for g in gaps if g["id"] == gid)
            miss_repos[gap["expected"]] += 1
    print("--- Top potential_miss repos (may need human review) ---")
    for repo, cnt in sorted(miss_repos.items(), key=lambda x: -x[1])[:15]:
        print(f"  {repo:45s} | {cnt} tasks")

    # Save to DB
    if save:
        for validation, gid in validations:
            conn.execute("UPDATE task_gaps SET validation = ? WHERE id = ?", (validation, gid))
        conn.commit()
        print(f"\n  Saved {len(validations)} validations to task_gaps.validation")

    conn.close()


if __name__ == "__main__":
    validate()
