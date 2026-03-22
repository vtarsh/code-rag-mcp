#!/usr/bin/env python3
"""Analyze developer-specific gap patterns from task_history + task_gaps.

Generates per-developer profiles showing which repos they tend to miss,
compared against team averages. Optionally saves to developer_patterns table.

Usage:
    python3 scripts/analyze_developer_patterns.py          # print only
    python3 scripts/analyze_developer_patterns.py --save    # save to DB
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__import__("os").getenv("CODE_RAG_HOME", Path.home() / ".pay-knowledge")) / "db" / "knowledge.db"

MIN_TASKS = 3
TOP_REPOS = 5


def get_connection():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def fetch_developer_tasks(conn):
    """Return {developer: [(ticket_id, repos_changed)]}."""
    cur = conn.execute("SELECT developer, ticket_id, repos_changed FROM task_history WHERE developer != ''")
    devs = defaultdict(list)
    for dev, ticket, repos_json in cur:
        repos = json.loads(repos_json) if repos_json else []
        devs[dev].append((ticket, repos))
    return devs


def fetch_gaps(conn):
    """Return {ticket_id: [(expected_repo, validation)]}."""
    cur = conn.execute("SELECT ticket_id, expected, validation FROM task_gaps WHERE gap_type = 'missing_repo'")
    gaps = defaultdict(list)
    for ticket, expected, validation in cur:
        gaps[ticket].append((expected, validation))
    return gaps


def get_project_prefix(ticket_id):
    return ticket_id.split("-")[0] if "-" in ticket_id else "UNKNOWN"


def analyze(conn):
    dev_tasks = fetch_developer_tasks(conn)
    all_gaps = fetch_gaps(conn)

    # --- Team-wide miss rates per repo ---
    team_repo_miss_count = defaultdict(int)  # repo -> total potential_miss across all devs
    team_repo_task_count = defaultdict(int)  # repo -> how many tasks could have missed it
    total_team_tasks = 0

    # --- Per-developer stats ---
    profiles = []

    for dev, tasks in sorted(dev_tasks.items(), key=lambda x: -len(x[1])):
        if len(tasks) < MIN_TASKS:
            continue

        total = len(tasks)
        total_team_tasks += total
        projects = set()
        total_gaps = 0
        real_misses = 0
        repo_miss_count = defaultdict(int)

        for ticket, _ in tasks:
            projects.add(get_project_prefix(ticket))
            ticket_gaps = all_gaps.get(ticket, [])
            total_gaps += len(ticket_gaps)
            for repo, validation in ticket_gaps:
                if validation == "potential_miss":
                    real_misses += 1
                    repo_miss_count[repo] += 1
                    team_repo_miss_count[repo] += 1

            # Count this dev's tasks toward each repo they missed
            for repo in repo_miss_count:
                team_repo_task_count[repo] += 0  # ensure key exists

        # Track task counts per repo for team average
        for repo in repo_miss_count:
            team_repo_task_count[repo] += total

        top_repos = sorted(repo_miss_count.items(), key=lambda x: -x[1])[:TOP_REPOS]

        profiles.append(
            {
                "developer": dev,
                "total_tasks": total,
                "total_gaps": total_gaps,
                "real_misses": real_misses,
                "projects": sorted(projects),
                "top_missed_repos": top_repos,
                "repo_miss_count": repo_miss_count,
            }
        )

    # --- Compute team averages and miss rates ---
    # Team average miss rate for a repo = total misses / total tasks of devs who ever missed it
    # But simpler: across ALL qualifying devs, how often does repo X get missed per task?
    all_qualifying_tasks = sum(p["total_tasks"] for p in profiles)

    # team miss rate per repo = team_repo_miss_count[repo] / all_qualifying_tasks
    team_miss_rate = {}
    for repo, count in team_repo_miss_count.items():
        team_miss_rate[repo] = count / all_qualifying_tasks if all_qualifying_tasks else 0

    # Attach miss_rate to each profile's repos
    for p in profiles:
        enriched = []
        for repo, count in p["top_missed_repos"]:
            dev_rate = count / p["total_tasks"]
            t_rate = team_miss_rate.get(repo, 0)
            ratio = dev_rate / t_rate if t_rate > 0 else 0
            enriched.append((repo, count, dev_rate, t_rate, ratio))
        p["top_missed_repos_enriched"] = enriched

    return profiles


def print_profiles(profiles):
    print("=" * 80)
    print("DEVELOPER GAP PATTERNS")
    print("=" * 80)

    for p in profiles:
        dev = p["developer"]
        projects_str = ", ".join(p["projects"])
        print(f"\n{'─' * 70}")
        print(f"  {dev}")
        print(
            f"  Tasks: {p['total_tasks']}  |  All gaps: {p['total_gaps']}  |  "
            f"Real misses: {p['real_misses']}  |  Projects: {projects_str}"
        )

        if not p["top_missed_repos_enriched"]:
            print("  No validated misses.")
            continue

        print(f"  {'Repo':<45} {'Cnt':>4} {'Dev%':>6} {'Team%':>6} {'Ratio':>6}")
        print(f"  {'─' * 67}")
        for repo, count, dr, tr, ratio in p["top_missed_repos_enriched"]:
            flag = " ***" if ratio >= 2.0 else " **" if ratio >= 1.5 else ""
            print(f"  {repo:<45} {count:>4} {dr:>5.1%} {tr:>5.1%} {ratio:>5.1f}x{flag}")

        # Generate suggestion sentence
        top = p["top_missed_repos_enriched"]
        if top:
            main_projects = ", ".join(p["projects"])
            repo_list = ", ".join(f"{r} ({c}x)" for r, c, *_ in top[:3])
            print(f"\n  >> {dev}: when working on {main_projects} tasks, often misses {repo_list}")

    print(f"\n{'=' * 80}")
    print("Legend: Ratio = developer miss rate / team average miss rate")
    print("  *** = 2x+ team average   ** = 1.5x+ team average")
    print(f"{'=' * 80}\n")


def save_to_db(conn, profiles):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS developer_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            developer TEXT,
            missed_repo TEXT,
            occurrences INTEGER,
            miss_rate REAL,
            projects TEXT,
            total_tasks INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Clear old data
    conn.execute("DELETE FROM developer_patterns")

    rows = []
    for p in profiles:
        projects_json = json.dumps(p["projects"])
        for repo, count, _dev_rate, _t_rate, ratio in p["top_missed_repos_enriched"]:
            rows.append(
                (
                    p["developer"],
                    repo,
                    count,
                    round(ratio, 2),
                    projects_json,
                    p["total_tasks"],
                )
            )

    conn.executemany(
        "INSERT INTO developer_patterns (developer, missed_repo, occurrences, miss_rate, projects, total_tasks) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    print(f"Saved {len(rows)} rows to developer_patterns table.")


def main():
    parser = argparse.ArgumentParser(description="Analyze developer gap patterns")
    parser.add_argument("--save", action="store_true", help="Save results to DB")
    args = parser.parse_args()

    conn = get_connection()
    profiles = analyze(conn)
    print_profiles(profiles)

    if args.save:
        save_to_db(conn, profiles)

    conn.close()


if __name__ == "__main__":
    main()
