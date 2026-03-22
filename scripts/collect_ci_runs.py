#!/usr/bin/env python3
"""Collect CI (GitHub Actions) run data from pay-com repos.

READ-ONLY: Only performs GET requests via gh API. Never writes to GitHub.

Usage:
    python3 scripts/collect_ci_runs.py              # collect last 10 runs per repo
    python3 scripts/collect_ci_runs.py --repos=5    # limit to 5 repos (for testing)
    python3 scripts/collect_ci_runs.py --dry-run    # print without saving
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

GH = "/opt/homebrew/bin/gh"
DB_PATH = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".pay-knowledge")) / "db" / "knowledge.db"
ORG = "pay-com"
MAX_REPOS = 30
RUNS_PER_REPO = 10
API_DELAY = 0.5  # seconds between API calls


def get_repos(db_path: Path, limit: int, offset: int = 0) -> list[str]:
    """Get repo names from knowledge DB."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT DISTINCT name FROM repos LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def fetch_runs(repo: str) -> tuple[list[dict] | None, str | None]:
    """Fetch recent workflow runs for a repo via gh API. Returns (runs, error)."""
    jq_filter = (
        f".workflow_runs[:{RUNS_PER_REPO}] | .[] | "
        "{id, name, status, conclusion, head_branch, created_at, updated_at}"
    )
    try:
        result = subprocess.run(
            [GH, "api", f"repos/{ORG}/{repo}/actions/runs", "--jq", jq_filter],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "404" in stderr or "Not Found" in stderr:
                return None, "404"
            if "rate limit" in stderr.lower():
                return None, "rate_limit"
            return None, stderr or f"exit code {result.returncode}"

        runs = []
        for line in result.stdout.strip().splitlines():
            if line.strip():
                runs.append(json.loads(line))
        return runs, None

    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, str(e)


def ensure_table(db_path: Path):
    """Create ci_runs table if it doesn't exist."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ci_runs (
                id INTEGER PRIMARY KEY,
                repo_name TEXT,
                workflow_name TEXT,
                branch TEXT,
                status TEXT,
                conclusion TEXT,
                created_at TEXT,
                updated_at TEXT,
                collected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(id)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def save_runs(db_path: Path, repo: str, runs: list[dict]) -> int:
    """Insert runs into DB. Returns count of new rows inserted."""
    conn = sqlite3.connect(str(db_path))
    inserted = 0
    try:
        for run in runs:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO ci_runs
                       (id, repo_name, workflow_name, branch, status, conclusion, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run["id"],
                        repo,
                        run.get("name"),
                        run.get("head_branch"),
                        run.get("status"),
                        run.get("conclusion"),
                        run.get("created_at"),
                        run.get("updated_at"),
                    ),
                )
                if conn.execute("SELECT changes()").fetchone()[0] > 0:
                    inserted += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
    finally:
        conn.close()
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Collect CI run data from pay-com repos")
    parser.add_argument("--repos", type=int, default=50, help="Max repos to query from DB")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N repos")
    parser.add_argument("--dry-run", action="store_true", help="Print without saving to DB")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    if not Path(GH).exists():
        print(f"ERROR: gh CLI not found at {GH}")
        sys.exit(1)

    # Cap repos at MAX_REPOS per run
    repo_limit = min(args.repos, MAX_REPOS)
    repos = get_repos(DB_PATH, repo_limit, args.offset)
    if not repos:
        print("No repos found in DB")
        sys.exit(1)

    if not args.dry_run:
        ensure_table(DB_PATH)

    print(f"Collecting CI runs from {len(repos)} repos (dry_run={args.dry_run})")
    print("-" * 60)

    stats = {"scanned": 0, "collected": 0, "failures": 0, "skipped_404": 0, "errors": 0}

    for i, repo in enumerate(repos):
        if i > 0:
            time.sleep(API_DELAY)

        runs, error = fetch_runs(repo)
        stats["scanned"] += 1

        if error == "404":
            stats["skipped_404"] += 1
            print(f"  [{i + 1}/{len(repos)}] {repo}: skipped (no actions)")
            continue
        elif error == "rate_limit":
            print(f"  [{i + 1}/{len(repos)}] {repo}: RATE LIMITED - stopping")
            break
        elif error:
            stats["errors"] += 1
            print(f"  [{i + 1}/{len(repos)}] {repo}: error - {error}")
            continue

        if not runs:
            print(f"  [{i + 1}/{len(repos)}] {repo}: 0 runs")
            continue

        failure_count = sum(1 for r in runs if r.get("conclusion") == "failure")
        stats["failures"] += failure_count
        stats["collected"] += len(runs)

        if args.dry_run:
            print(f"  [{i + 1}/{len(repos)}] {repo}: {len(runs)} runs ({failure_count} failures) [dry-run]")
            for r in runs[:3]:
                branch = r.get("head_branch", "?")
                conclusion = r.get("conclusion", r.get("status", "?"))
                print(f"    - {r.get('name', '?')} @ {branch}: {conclusion}")
            if len(runs) > 3:
                print(f"    ... and {len(runs) - 3} more")
        else:
            inserted = save_runs(DB_PATH, repo, runs)
            print(f"  [{i + 1}/{len(repos)}] {repo}: {len(runs)} runs, {inserted} new ({failure_count} failures)")

    print("-" * 60)
    print(f"Repos scanned:   {stats['scanned']}")
    print(f"Skipped (404):   {stats['skipped_404']}")
    print(f"Errors:          {stats['errors']}")
    print(f"Runs collected:  {stats['collected']}")
    print(f"Failures found:  {stats['failures']}")


if __name__ == "__main__":
    main()
