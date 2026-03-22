#!/usr/bin/env python3
"""
Phase 9: Auto-collect recently closed Jira tasks.

Queries Jira for recently resolved tickets, collects new ones,
runs cross-validation, and updates patterns. Designed for launchd scheduling.

Usage:
    python scripts/auto_collect.py                    # collect PI-* from last 7 days
    python scripts/auto_collect.py --days=30          # last 30 days
    python scripts/auto_collect.py --projects=PI,CORE # multiple projects
    python scripts/auto_collect.py --dry-run          # show what would be collected
"""

import json
import os
import sqlite3
import sys
import urllib.request
from base64 import b64encode
from datetime import UTC, datetime
from pathlib import Path

# Re-use existing scripts
_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

from scripts.analyze_gaps import analyze  # noqa: E402
from scripts.collect_task import JIRA_DOMAIN, JIRA_EMAIL, JIRA_TOKEN, collect  # noqa: E402
from scripts.cross_validate_task import cross_validate  # noqa: E402

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"

# Jira statuses that indicate "done enough to collect"
DONE_STATUSES = ["Done", "Deployed", "Testing", "Ready to deploy", "Code Review"]


def _jira_search(jql: str, max_results: int = 50) -> list[dict]:
    """Search Jira using the new /search/jql endpoint."""
    url = f"https://{JIRA_DOMAIN}/rest/api/3/search/jql"
    cred = b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    headers = {
        "Authorization": f"Basic {cred}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = json.dumps(
        {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["key", "summary", "status", "issuetype", "assignee"],
        }
    ).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("issues", [])
    except Exception as e:
        print(f"  [error] Jira search failed: {e}")
        return []


def get_already_collected() -> set[str]:
    """Get ticket IDs already in task_history."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute("SELECT ticket_id FROM task_history").fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def find_new_tickets(projects: list[str], days: int) -> list[dict]:
    """Find recently resolved tickets not yet collected."""
    already = get_already_collected()
    status_list = ", ".join(f'"{s}"' for s in DONE_STATUSES)
    project_list = ", ".join(projects)

    jql = (
        f"project IN ({project_list}) "
        f"AND status IN ({status_list}) "
        f"AND updated >= -{days}d "
        f"AND status != Draft "
        f"ORDER BY updated DESC"
    )
    print(f"  JQL: {jql}")
    issues = _jira_search(jql, max_results=50)

    new_tickets = []
    for issue in issues:
        key = issue["key"]
        if key not in already:
            f = issue["fields"]
            assignee = (f.get("assignee") or {}).get("displayName", "?")
            status = f["status"]["name"]
            summary = f["summary"]
            new_tickets.append(
                {
                    "key": key,
                    "summary": summary,
                    "status": status,
                    "assignee": assignee,
                }
            )

    return new_tickets


def _save_run_log(run_log: dict):
    """Save structured run log to DB for morning review."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auto_collect_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            projects TEXT,
            days INTEGER,
            already_collected INTEGER,
            new_found INTEGER,
            collected_ok INTEGER,
            collect_errors INTEGER,
            cross_validated INTEGER,
            cv_errors INTEGER,
            patterns_count INTEGER,
            total_tasks INTEGER,
            total_gaps INTEGER,
            error_details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        """INSERT INTO auto_collect_runs
           (started_at, finished_at, projects, days, already_collected, new_found,
            collected_ok, collect_errors, cross_validated, cv_errors,
            patterns_count, total_tasks, total_gaps, error_details)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_log["started_at"],
            run_log["finished_at"],
            run_log["projects"],
            run_log["days"],
            run_log["already_collected"],
            run_log["new_found"],
            run_log["collected_ok"],
            run_log["collect_errors"],
            run_log["cross_validated"],
            run_log["cv_errors"],
            run_log["patterns_count"],
            run_log["total_tasks"],
            run_log["total_gaps"],
            json.dumps(run_log["errors"]) if run_log["errors"] else None,
        ),
    )
    conn.commit()
    conn.close()


def auto_collect(projects: list[str], days: int, *, dry_run: bool = False):
    """Main auto-collection loop with structured logging."""
    started = datetime.now(UTC).isoformat()
    print(f"=== Auto-Collect: projects={projects}, last {days} days ===\n")

    run_log = {
        "started_at": started,
        "finished_at": "",
        "projects": ",".join(projects),
        "days": days,
        "already_collected": 0,
        "new_found": 0,
        "collected_ok": 0,
        "collect_errors": 0,
        "cross_validated": 0,
        "cv_errors": 0,
        "patterns_count": 0,
        "total_tasks": 0,
        "total_gaps": 0,
        "errors": [],
    }

    # 1. Find new tickets
    try:
        new_tickets = find_new_tickets(projects, days)
    except Exception as e:
        run_log["errors"].append(f"Jira search failed: {e}")
        run_log["finished_at"] = datetime.now(UTC).isoformat()
        if not dry_run:
            _save_run_log(run_log)
        print(f"  [FATAL] Jira search failed: {e}")
        return

    already = get_already_collected()
    run_log["already_collected"] = len(already)
    run_log["new_found"] = len(new_tickets)
    print(f"  Already collected: {len(already)} tasks")
    print(f"  New tickets found: {len(new_tickets)}\n")

    if not new_tickets:
        print("  Nothing new to collect.")
        run_log["finished_at"] = datetime.now(UTC).isoformat()
        if not dry_run:
            _save_run_log(run_log)
        return

    for t in new_tickets:
        print(f"  NEW: {t['key']:8s} | {t['status']:15s} | {t['assignee']:25s} | {t['summary'][:50]}")

    if dry_run:
        print(f"\n  [dry-run] Would collect {len(new_tickets)} tasks.")
        return

    # 2. Collect each new ticket
    print(f"\n--- Collecting {len(new_tickets)} tasks ---\n")
    for t in new_tickets:
        try:
            collect(t["key"], force=False)
            run_log["collected_ok"] += 1
        except Exception as e:
            run_log["collect_errors"] += 1
            run_log["errors"].append(f"collect {t['key']}: {e}")
            print(f"  [error] {t['key']}: {e}")

    # 3. Cross-validate new tasks
    print(f"\n--- Cross-validating {run_log['collected_ok']} new tasks ---\n")
    for t in new_tickets:
        try:
            cross_validate(t["key"])
            run_log["cross_validated"] += 1
        except Exception as e:
            run_log["cv_errors"] += 1
            run_log["errors"].append(f"cross-validate {t['key']}: {e}")
            print(f"  [error] cross-validate {t['key']}: {e}")

    # 4. Update patterns
    print("\n--- Updating patterns ---\n")
    try:
        analyze()
    except Exception as e:
        run_log["errors"].append(f"analyze: {e}")

    # 5. Final stats
    conn = sqlite3.connect(str(DB_PATH))
    run_log["total_tasks"] = conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
    run_log["total_gaps"] = conn.execute("SELECT COUNT(*) FROM task_gaps").fetchone()[0]
    run_log["patterns_count"] = conn.execute(
        "SELECT COUNT(*) FROM task_patterns"
        if "task_patterns"
        in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        else "SELECT 0"
    ).fetchone()[0]
    conn.close()

    run_log["finished_at"] = datetime.now(UTC).isoformat()
    _save_run_log(run_log)

    ok = run_log["collected_ok"]
    errs = run_log["collect_errors"] + run_log["cv_errors"]
    print(f"\n=== Done: {ok} collected, {errs} errors, {run_log['total_tasks']} total tasks ===")


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    days = 7
    projects = ["PI"]

    for arg in args:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
        elif arg.startswith("--projects="):
            projects = arg.split("=")[1].split(",")

    auto_collect(projects, days, dry_run=dry_run)


if __name__ == "__main__":
    main()
