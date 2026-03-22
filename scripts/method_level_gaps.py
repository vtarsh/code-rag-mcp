#!/usr/bin/env python3
"""
Method-level gap analysis: re-score task_gaps using method implementation data.

For each HIGH confidence gap (potential_miss), checks whether the expected repo
implements the same methods that the task touched. If not, the gap is likely
a false positive.

Requires: method_matrix table (run build_method_matrix.py --save first).

Usage:
    python scripts/method_level_gaps.py          # dry-run, print only
    python scripts/method_level_gaps.py --save   # annotate gaps in DB
"""

import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"

# Confidence threshold for "HIGH" gaps worth analyzing
CONFIDENCE_THRESHOLD = 0.7


_SKIP_NAMES = {
    "index",
    "example",
    "example-activity",
    "types",
    "constants",
    "consts",
    "config",
    "utils",
    "helpers",
    "common",
}


def extract_methods_from_files(files_changed: list[str]) -> set[str]:
    """Extract method/function names from files_changed paths.

    files_changed entries look like:
      "repo-name/methods/verification.js"
      "repo-name/workflows/activities/trustly/handle-activities.js"
      "repo-name/libs/map-response.js"
      "repo-name/workflows/src/activities/fetch-payment-transaction.ts"
    """
    methods = set()
    for fp in files_changed:
        # Skip test/spec files
        if ".spec." in fp or "/test" in fp:
            continue

        # methods/*.js
        m = re.search(r"methods/([^/]+)\.(js|ts)$", fp)
        if m:
            name = m.group(1)
            if name not in _SKIP_NAMES:
                methods.add(name)
            continue

        # workflows/activities/{provider}/{name}.js or workflows/activities/{name}.js
        m = re.search(r"workflows/(?:src/)?activities/(?:([^/]+)/)?([^/]+)\.(js|ts)$", fp)
        if m:
            provider = m.group(1)
            name = m.group(2)
            if name not in _SKIP_NAMES:
                methods.add(f"{provider}/{name}" if provider else name)
            continue

        # libs/{subdir}/{name}.js or libs/{name}.js
        m = re.search(r"libs/(?:([^/]+)/)?([^/]+)\.(js|ts)$", fp)
        if m:
            subdir = m.group(1)
            name = m.group(2)
            if name not in _SKIP_NAMES:
                methods.add(f"{subdir}/{name}" if subdir else name)
            continue

        # workflows/src/workflows/{name}.ts
        m = re.search(r"workflows/(?:src/)?workflows/([^/]+)\.(js|ts)$", fp)
        if m:
            name = m.group(1)
            if name not in _SKIP_NAMES:
                methods.add(name)
            continue

    return methods


def load_method_matrix(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Load method_matrix as repo_name → set of method names."""
    matrix: dict[str, set[str]] = defaultdict(set)
    rows = conn.execute("SELECT repo_name, method_name FROM method_matrix").fetchall()
    for repo_name, method_name in rows:
        matrix[repo_name].add(method_name)
    return dict(matrix)


def ensure_column(conn: sqlite3.Connection):
    """Add method_relevance column to task_gaps if it doesn't exist."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(task_gaps)").fetchall()]
    if "method_relevance" not in cols:
        conn.execute("ALTER TABLE task_gaps ADD COLUMN method_relevance TEXT DEFAULT NULL")
        conn.commit()


def analyze_gaps(conn: sqlite3.Connection) -> list[dict]:
    """Analyze each high-confidence gap for method relevance."""
    matrix = load_method_matrix(conn)

    if not matrix:
        print("ERROR: method_matrix is empty. Run build_method_matrix.py --save first.")
        sys.exit(1)

    # Get high-confidence missing_repo gaps
    gaps = conn.execute(
        "SELECT g.id, g.ticket_id, g.expected, g.actual, g.confidence "
        "FROM task_gaps g "
        "WHERE g.gap_type = 'missing_repo' AND g.confidence >= ?",
        (CONFIDENCE_THRESHOLD,),
    ).fetchall()

    # Pre-load task files_changed
    task_files: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT ticket_id, files_changed FROM task_history WHERE files_changed IS NOT NULL"
    ).fetchall():
        try:
            task_files[row[0]] = json.loads(row[1]) if row[1] else []
        except (json.JSONDecodeError, TypeError):
            task_files[row[0]] = []

    results = []
    for gap_id, ticket_id, expected_repo, _actual_json, confidence in gaps:
        files = task_files.get(ticket_id, [])
        task_methods = extract_methods_from_files(files)

        if not task_methods or expected_repo not in matrix:
            relevance = "no_method_data"
        else:
            repo_methods = matrix[expected_repo]
            overlap = task_methods & repo_methods
            if overlap:
                relevance = "relevant"
            else:
                relevance = "irrelevant"

        results.append(
            {
                "gap_id": gap_id,
                "ticket_id": ticket_id,
                "expected_repo": expected_repo,
                "task_methods": sorted(task_methods),
                "repo_methods": sorted(matrix.get(expected_repo, set())),
                "relevance": relevance,
                "confidence": confidence,
            }
        )

    return results


def save_annotations(conn: sqlite3.Connection, results: list[dict]):
    """Write method_relevance back to task_gaps."""
    ensure_column(conn)
    for r in results:
        conn.execute(
            "UPDATE task_gaps SET method_relevance = ? WHERE id = ?",
            (r["relevance"], r["gap_id"]),
        )
    conn.commit()


def print_stats(results: list[dict]):
    """Print analysis summary."""
    total = len(results)
    relevance_counts = Counter(r["relevance"] for r in results)
    relevant = relevance_counts.get("relevant", 0)
    irrelevant = relevance_counts.get("irrelevant", 0)
    no_data = relevance_counts.get("no_method_data", 0)

    print("\n=== Method-Level Gap Analysis ===\n")
    print(f"  Total gaps analyzed (confidence >= {CONFIDENCE_THRESHOLD}): {total}")
    print(f"  METHOD_RELEVANT:    {relevant:4d}  ({relevant * 100 // max(total, 1)}%)")
    print(f"  METHOD_IRRELEVANT:  {irrelevant:4d}  ({irrelevant * 100 // max(total, 1)}%)")
    print(f"  NO_METHOD_DATA:     {no_data:4d}  ({no_data * 100 // max(total, 1)}%)")

    if irrelevant > 0 and total > 0:
        fp_reduction = irrelevant * 100 // total
        print(f"\n  Potential false-positive reduction: {fp_reduction}% of high-confidence gaps")

    # Top repos by method relevance
    repo_relevant = Counter()
    repo_irrelevant = Counter()
    for r in results:
        if r["relevance"] == "relevant":
            repo_relevant[r["expected_repo"]] += 1
        elif r["relevance"] == "irrelevant":
            repo_irrelevant[r["expected_repo"]] += 1

    if repo_relevant:
        print("\n  Top repos with METHOD_RELEVANT gaps:")
        for repo, cnt in repo_relevant.most_common(10):
            print(f"    {repo:45s} {cnt}")

    if repo_irrelevant:
        print("\n  Top repos with METHOD_IRRELEVANT gaps (likely false positives):")
        for repo, cnt in repo_irrelevant.most_common(10):
            print(f"    {repo:45s} {cnt}")

    # Show a few example relevant gaps
    relevant_examples = [r for r in results if r["relevance"] == "relevant"][:5]
    if relevant_examples:
        print("\n  Example METHOD_RELEVANT gaps:")
        for r in relevant_examples:
            overlap = set(r["task_methods"]) & set(r["repo_methods"])
            print(f"    {r['ticket_id']} → {r['expected_repo']} (shared methods: {', '.join(sorted(overlap))})")

    # Show a few example irrelevant gaps
    irrelevant_examples = [r for r in results if r["relevance"] == "irrelevant"][:5]
    if irrelevant_examples:
        print("\n  Example METHOD_IRRELEVANT gaps:")
        for r in irrelevant_examples:
            print(
                f"    {r['ticket_id']} → {r['expected_repo']} "
                f"(task methods: {', '.join(r['task_methods'][:3])} | "
                f"repo methods: {', '.join(r['repo_methods'][:3])})"
            )


def main():
    save = "--save" in sys.argv

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        # Verify method_matrix exists
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='method_matrix'"
        ).fetchone()
        if not table_check:
            print("ERROR: method_matrix table not found. Run build_method_matrix.py --save first.")
            sys.exit(1)

        results = analyze_gaps(conn)
        print_stats(results)

        if save:
            save_annotations(conn, results)
            print(f"\n  Saved {len(results)} method_relevance annotations to task_gaps.")
        else:
            print("\n  Dry run — use --save to persist.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
