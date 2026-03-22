#!/usr/bin/env python3
"""Export pay-knowledge task patterns to portable JSON for CI consumption."""

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path.home() / ".pay-knowledge" / "db" / "knowledge.db"
OUTPUT_PATH = Path.home() / ".pay-knowledge" / "patterns-export.json"
MIN_OCCURRENCES = 3


def main():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Gather stats
    total_tasks = conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
    total_gaps = conn.execute("SELECT COUNT(*) FROM task_gaps").fetchone()[0]
    total_patterns = conn.execute("SELECT COUNT(*) FROM task_patterns").fetchone()[0]

    # Load patterns by type, filtered by min occurrences
    rows = conn.execute(
        "SELECT * FROM task_patterns WHERE occurrences >= ? ORDER BY occurrences DESC",
        (MIN_OCCURRENCES,),
    ).fetchall()
    conn.close()

    upstream_callers = []
    co_occurrences = []
    clusters = []

    for row in rows:
        ptype = row["pattern_type"]
        trigger_repos = json.loads(row["trigger_repos"]) if row["trigger_repos"] else []

        if ptype == "upstream_caller":
            upstream_callers.append(
                {
                    "repo": row["missed_repo"],
                    "missed_in_tasks": row["occurrences"],
                    "confidence": round(row["confidence"], 4),
                }
            )
        elif ptype == "co_occurrence":
            co_occurrences.append(
                {
                    "trigger_repos": trigger_repos,
                    "missed_repo": row["missed_repo"],
                    "occurrences": row["occurrences"],
                }
            )
        elif ptype == "cluster":
            clusters.append(
                {
                    "repo_a": row["missed_repo"],
                    "repo_b": trigger_repos[0] if trigger_repos else None,
                    "co_missed_in": row["occurrences"],
                }
            )

    export = {
        "version": "1.0",
        "exported_at": datetime.now(UTC).isoformat(),
        "stats": {
            "total_tasks": total_tasks,
            "total_gaps": total_gaps,
            "total_patterns": total_patterns,
        },
        "upstream_callers": upstream_callers,
        "co_occurrences": co_occurrences,
        "clusters": clusters,
    }

    OUTPUT_PATH.write_text(json.dumps(export, indent=2) + "\n")

    # Print summary
    print(f"Exported patterns to {OUTPUT_PATH}")
    print(f"  Tasks: {total_tasks}  Gaps: {total_gaps}  Patterns: {total_patterns}")
    print(f"  Upstream callers (>={MIN_OCCURRENCES}): {len(upstream_callers)}")
    print(f"  Co-occurrences  (>={MIN_OCCURRENCES}): {len(co_occurrences)}")
    print(f"  Clusters        (>={MIN_OCCURRENCES}): {len(clusters)}")


if __name__ == "__main__":
    main()
