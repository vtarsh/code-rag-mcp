#!/usr/bin/env python3
"""
Analyze directory-level co-occurrence patterns from task_history.

Discovers which directories (repo + first subdir) change together across tasks,
identifies hub files that appear in many tasks, and finds cross-repo file patterns.

Usage:
    python3 scripts/analyze_file_patterns.py          # print only
    python3 scripts/analyze_file_patterns.py --save    # save to DB
"""

import json
import os
import re
import sqlite3
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"

MIN_DIR_COOCCURRENCE = 3
MIN_HUB_FILE_TASKS = 5
MIN_CROSS_REPO_FILE = 3


def get_connection():
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(str(DB_PATH))


def is_test_file(path: str) -> bool:
    """Check if a file path is a test/spec file."""
    return bool(re.search(r"\.(spec|test)\.", path))


def normalize_to_dir(path: str) -> str:
    """Normalize 'repo/subdir/file.js' -> 'repo/subdir/'."""
    parts = path.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}/"
    return f"{parts[0]}/"


def get_repo(path: str) -> str:
    return path.split("/")[0]


def load_tasks(conn):
    """Load all tasks with non-empty files_changed."""
    cur = conn.execute(
        "SELECT ticket_id, files_changed FROM task_history WHERE files_changed IS NOT NULL AND files_changed != '[]'"
    )
    tasks = []
    for ticket_id, files_json in cur.fetchall():
        try:
            files = json.loads(files_json)
            if isinstance(files, list) and files:
                tasks.append((ticket_id, files))
        except (json.JSONDecodeError, TypeError):
            continue
    return tasks


def analyze_dir_pairs(tasks):
    """Build directory-pair co-occurrence matrix (cross-repo only, no test files)."""
    pair_counts = Counter()
    dir_task_counts = Counter()

    for _ticket_id, files in tasks:
        # Filter test files, normalize to dirs
        dirs = set()
        for f in files:
            if not is_test_file(f):
                dirs.add(normalize_to_dir(f))

        # Count per-dir appearances
        for d in dirs:
            dir_task_counts[d] += 1

        # Cross-repo pairs only
        for a, b in combinations(sorted(dirs), 2):
            if get_repo(a.rstrip("/")) != get_repo(b.rstrip("/")):
                pair_counts[(a, b)] += 1

    # Filter by minimum co-occurrences
    results = []
    for (a, b), count in pair_counts.most_common():
        if count < MIN_DIR_COOCCURRENCE:
            break
        # Confidence = co-occurrences / min(individual occurrences)
        min_individual = min(dir_task_counts[a], dir_task_counts[b])
        confidence = count / min_individual if min_individual > 0 else 0.0
        results.append(
            {
                "source": a,
                "target": b,
                "occurrences": count,
                "confidence": round(confidence, 3),
            }
        )

    return results


def analyze_hub_files(tasks):
    """Find files that appear in many tasks."""
    file_task_counts = Counter()

    for _ticket_id, files in tasks:
        seen = set()
        for f in files:
            if not is_test_file(f) and f not in seen:
                file_task_counts[f] += 1
                seen.add(f)

    results = []
    for filepath, count in file_task_counts.most_common():
        if count < MIN_HUB_FILE_TASKS:
            break
        results.append(
            {
                "source": filepath,
                "target": "",
                "occurrences": count,
                "confidence": round(count / len(tasks), 3),
            }
        )

    return results


def analyze_cross_repo_files(tasks):
    """Find specific file-to-file patterns across repos."""
    file_pair_counts = Counter()
    file_task_counts = Counter()

    for _ticket_id, files in tasks:
        non_test = [f for f in files if not is_test_file(f)]
        seen = set()
        for f in non_test:
            if f not in seen:
                file_task_counts[f] += 1
                seen.add(f)

        for a, b in combinations(sorted(set(non_test)), 2):
            if get_repo(a) != get_repo(b):
                file_pair_counts[(a, b)] += 1

    results = []
    for (a, b), count in file_pair_counts.most_common():
        if count < MIN_CROSS_REPO_FILE:
            break
        min_individual = min(file_task_counts[a], file_task_counts[b])
        confidence = count / min_individual if min_individual > 0 else 0.0
        results.append(
            {
                "source": a,
                "target": b,
                "occurrences": count,
                "confidence": round(confidence, 3),
            }
        )

    return results


def print_results(dir_pairs, hub_files, cross_repo_files):
    print("=" * 70)
    print("FILE CO-OCCURRENCE PATTERN ANALYSIS")
    print("=" * 70)

    print(f"\n--- Top Directory Pairs (min {MIN_DIR_COOCCURRENCE} co-occurrences) ---")
    if dir_pairs:
        for i, r in enumerate(dir_pairs[:30], 1):
            print(f"  {i:2d}. {r['source']}  <->  {r['target']}")
            print(f"      occurrences={r['occurrences']}  confidence={r['confidence']:.1%}")
    else:
        print("  (none found)")

    print(f"\n--- Hub Files (changed in {MIN_HUB_FILE_TASKS}+ tasks) ---")
    if hub_files:
        for i, r in enumerate(hub_files[:20], 1):
            print(f"  {i:2d}. {r['source']}  ({r['occurrences']} tasks, {r['confidence']:.1%})")
    else:
        print("  (none found)")

    print(f"\n--- Cross-Repo File Patterns (min {MIN_CROSS_REPO_FILE} co-occurrences) ---")
    if cross_repo_files:
        for i, r in enumerate(cross_repo_files[:30], 1):
            print(f"  {i:2d}. {r['source']}")
            print(f"      <-> {r['target']}")
            print(f"      occurrences={r['occurrences']}  confidence={r['confidence']:.1%}")
    else:
        print("  (none found)")

    print("\n--- Summary ---")
    print(f"  Directory pairs:       {len(dir_pairs)}")
    print(f"  Hub files:             {len(hub_files)}")
    print(f"  Cross-repo file pairs: {len(cross_repo_files)}")
    total = len(dir_pairs) + len(hub_files) + len(cross_repo_files)
    print(f"  Total patterns:        {total}")


def save_to_db(conn, dir_pairs, hub_files, cross_repo_files):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_type TEXT,
            source TEXT,
            target TEXT,
            occurrences INTEGER,
            confidence REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Clear old patterns
    conn.execute("DELETE FROM file_patterns")

    rows = []
    for r in dir_pairs:
        rows.append(("dir_pair", r["source"], r["target"], r["occurrences"], r["confidence"]))
    for r in hub_files:
        rows.append(("hub_file", r["source"], r["target"], r["occurrences"], r["confidence"]))
    for r in cross_repo_files:
        rows.append(("cross_repo_file", r["source"], r["target"], r["occurrences"], r["confidence"]))

    conn.executemany(
        "INSERT INTO file_patterns (pattern_type, source, target, occurrences, confidence) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    print(f"\nSaved {len(rows)} patterns to file_patterns table.")


def main():
    save = "--save" in sys.argv

    conn = get_connection()
    tasks = load_tasks(conn)
    print(f"Loaded {len(tasks)} tasks with files_changed.\n")

    if not tasks:
        print("No tasks found. Exiting.")
        return

    dir_pairs = analyze_dir_pairs(tasks)
    hub_files = analyze_hub_files(tasks)
    cross_repo_files = analyze_cross_repo_files(tasks)

    print_results(dir_pairs, hub_files, cross_repo_files)

    if save:
        save_to_db(conn, dir_pairs, hub_files, cross_repo_files)

    conn.close()


if __name__ == "__main__":
    main()
