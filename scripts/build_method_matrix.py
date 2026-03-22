#!/usr/bin/env python3
"""
Build method_matrix: maps repos → methods/functions they implement.

Data sources (priority order):
1. chunks table — methods/*.js files (excluding spec, index)
2. chunks table — activities (Temporal workflow activities)
3. chunks table — libs (utility functions, mappers, payload builders)
4. chunks table — workflows (workflow definitions)
5. code_facts table — validation/cross-reference
6. providers-proto — canonical RPC method list

Usage:
    python scripts/build_method_matrix.py          # dry-run, print only
    python scripts/build_method_matrix.py --save   # save to DB
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"


def extract_method_name(file_path: str) -> str | None:
    """Extract method name from path like methods/verification.js → verification."""
    m = re.match(r"methods/([^/]+)\.js$", file_path)
    if not m:
        return None
    name = m.group(1)
    # Skip index.js — it's just the barrel export
    if name == "index":
        return None
    return name


# Skip patterns for non-meaningful file names
_SKIP_NAMES = {"index", "example-activity", "types", "constants", "consts", "config", "utils"}


def extract_activity_name(file_path: str) -> str | None:
    """Extract activity name from Temporal activity paths.

    Patterns:
      workflows/activities/get-transaction-details.js → get-transaction-details
      workflows/activities/trustly/handle-activities.js → trustly/handle-activities
      workflows/src/activities/fetch-payment-transaction.ts → fetch-payment-transaction
      workflows/src/activities/chargebacks911/create-signal.ts → chargebacks911/create-signal
    """
    # JS pattern: workflows/activities/{name}.js or workflows/activities/{provider}/{name}.js
    m = re.match(r"workflows/activities/(?:([^/]+)/)?([^/]+)\.(js|ts)$", file_path)
    if not m:
        # TS pattern: workflows/src/activities/{name}.ts or workflows/src/activities/{provider}/{name}.ts
        m = re.match(r"workflows/src/activities/(?:([^/]+)/)?([^/]+)\.(js|ts)$", file_path)
    if not m:
        return None

    provider_or_dir = m.group(1)  # may be None
    name = m.group(2)
    if name in _SKIP_NAMES:
        return None
    if provider_or_dir:
        return f"{provider_or_dir}/{name}"
    return name


def extract_lib_name(file_path: str) -> str | None:
    """Extract function/module name from libs paths.

    Patterns:
      libs/map-response.js → map-response
      libs/payload-builders/get-sale-payload.js → payload-builders/get-sale-payload
      libs/gumballpay/gumballpay-client.js → gumballpay/gumballpay-client
    """
    m = re.match(r"libs/(?:([^/]+)/)?([^/]+)\.(js|ts)$", file_path)
    if not m:
        return None
    subdir = m.group(1)  # may be None
    name = m.group(2)
    if name in _SKIP_NAMES:
        return None
    if subdir:
        return f"{subdir}/{name}"
    return name


def extract_workflow_name(file_path: str) -> str | None:
    """Extract workflow definition name.

    Patterns:
      workflows/workflow.js → workflow
      workflows/src/workflow.ts → workflow
      workflows/src/workflows/chargeback911-task-workflow.ts → chargeback911-task-workflow
    """
    # workflows/src/workflows/{name}.ts
    m = re.match(r"workflows/src/workflows/([^/]+)\.(js|ts)$", file_path)
    if m:
        name = m.group(1)
        return None if name in _SKIP_NAMES else name
    # workflows/workflow.js or workflows/src/workflow.ts
    m = re.match(r"workflows/(?:src/)?([^/]+)\.(js|ts)$", file_path)
    if m:
        name = m.group(1)
        return None if name in _SKIP_NAMES else name
    return None


def parse_proto_methods(content: str) -> list[str]:
    """Parse RPC method names from proto service definition."""
    return re.findall(r"rpc\s+(\w+)\s*\(", content)


def build_matrix(conn: sqlite3.Connection) -> list[dict]:
    """Build the method matrix from all data sources."""
    # Track unique (repo, method, source) → file_path
    # Use (repo, method, source) as key to allow same repo to have entries
    # from different source types (methods/, libs/, activities/, etc.)
    matrix: dict[tuple[str, str, str], str] = {}

    def _add(repo: str, method: str, source: str, fpath: str):
        key = (repo, method, source)
        if key not in matrix:
            matrix[key] = fpath

    # --- Source 1: methods/*.js from chunks (highest priority) ---
    rows = conn.execute(
        "SELECT DISTINCT repo_name, file_path FROM chunks "
        "WHERE file_path LIKE 'methods/%.js' AND file_path NOT LIKE '%.spec.%'"
    ).fetchall()
    for repo_name, file_path in rows:
        method = extract_method_name(file_path)
        if method:
            _add(repo_name, method, "chunks", file_path)

    # --- Source 2: activities (Temporal workflow activities) ---
    rows = conn.execute(
        "SELECT DISTINCT repo_name, file_path FROM chunks "
        "WHERE (file_path LIKE 'workflows/activities/%.js' "
        "    OR file_path LIKE 'workflows/activities/%.ts' "
        "    OR file_path LIKE 'workflows/src/activities/%.js' "
        "    OR file_path LIKE 'workflows/src/activities/%.ts') "
        "  AND file_path NOT LIKE '%.spec.%' "
        "  AND repo_name NOT LIKE 'boilerplate%'"
    ).fetchall()
    for repo_name, file_path in rows:
        name = extract_activity_name(file_path)
        if name:
            _add(repo_name, name, "activities", file_path)

    # --- Source 3: libs/ (utility functions, mappers, payload builders) ---
    rows = conn.execute(
        "SELECT DISTINCT repo_name, file_path FROM chunks "
        "WHERE file_path LIKE 'libs/%.js' OR file_path LIKE 'libs/%.ts' "
        "  AND file_path NOT LIKE '%.spec.%' "
        "  AND file_path NOT LIKE '%test%' "
        "  AND repo_name NOT LIKE 'boilerplate%'"
    ).fetchall()
    for repo_name, file_path in rows:
        name = extract_lib_name(file_path)
        if name:
            _add(repo_name, name, "libs", file_path)

    # --- Source 4: workflows/ (workflow definitions) ---
    rows = conn.execute(
        "SELECT DISTINCT repo_name, file_path FROM chunks "
        "WHERE (file_path LIKE 'workflows/workflow.%' "
        "    OR file_path LIKE 'workflows/src/workflow.%' "
        "    OR file_path LIKE 'workflows/src/workflows/%.js' "
        "    OR file_path LIKE 'workflows/src/workflows/%.ts') "
        "  AND file_path NOT LIKE '%.spec.%' "
        "  AND repo_name NOT LIKE 'boilerplate%'"
    ).fetchall()
    for repo_name, file_path in rows:
        name = extract_workflow_name(file_path)
        if name:
            _add(repo_name, name, "workflows", file_path)

    # --- Source 5: code_facts table (fills gaps for methods/) ---
    rows = conn.execute(
        "SELECT DISTINCT repo_name, file_path FROM code_facts WHERE file_path LIKE 'methods/%.js'"
    ).fetchall()
    for repo_name, file_path in rows:
        method = extract_method_name(file_path)
        if method:
            _add(repo_name, method, "code_facts", file_path)

    # --- Source 6: proto canonical list ---
    proto_rows = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = 'providers-proto' AND chunk_type = 'proto_service'"
    ).fetchall()
    for (content,) in proto_rows:
        for method in parse_proto_methods(content):
            _add("providers-proto", method, "proto", "proto/provider.proto")

    # Convert to list
    results = []
    for (repo_name, method_name, source), file_path in sorted(matrix.items()):
        results.append(
            {
                "repo_name": repo_name,
                "method_name": method_name,
                "source": source,
                "file_path": file_path,
            }
        )
    return results


def save_matrix(conn: sqlite3.Connection, rows: list[dict]):
    """Create/recreate method_matrix table and insert rows."""
    conn.execute("DROP TABLE IF EXISTS method_matrix")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS method_matrix (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_name TEXT,
            method_name TEXT,
            source TEXT,
            file_path TEXT,
            UNIQUE(repo_name, method_name, source)
        )
    """)
    conn.executemany(
        "INSERT OR IGNORE INTO method_matrix (repo_name, method_name, source, file_path) "
        "VALUES (:repo_name, :method_name, :source, :file_path)",
        rows,
    )
    conn.commit()


def print_stats(rows: list[dict]):
    """Print summary statistics."""
    repos = set()
    methods = set()
    source_counts = defaultdict(int)
    repo_method_counts = defaultdict(int)

    for r in rows:
        repos.add(r["repo_name"])
        methods.add(r["method_name"])
        source_counts[r["source"]] += 1
        repo_method_counts[r["repo_name"]] += 1

    print("\n=== Method Matrix Stats ===\n")
    print(f"  Total entries:    {len(rows)}")
    print(f"  Unique repos:     {len(repos)}")
    print(f"  Unique methods:   {len(methods)}")
    print("\n  By source:")
    for src, cnt in sorted(source_counts.items(), key=lambda x: -x[1]):
        print(f"    {src:15s} {cnt}")

    # Unique repos per source
    repos_by_source: dict[str, set] = defaultdict(set)
    for r in rows:
        repos_by_source[r["source"]].add(r["repo_name"])
    print("\n  Unique repos per source:")
    for src, repo_set in sorted(repos_by_source.items(), key=lambda x: -len(x[1])):
        print(f"    {src:15s} {len(repo_set)} repos")

    # Proto canonical methods
    proto_methods = sorted({r["method_name"] for r in rows if r["source"] == "proto"})
    if proto_methods:
        print(f"\n  Proto canonical methods ({len(proto_methods)}):")
        print(f"    {', '.join(proto_methods)}")

    # Top repos by method count
    top_repos = sorted(repo_method_counts.items(), key=lambda x: -x[1])[:15]
    print("\n  Top repos by method count:")
    for repo, cnt in top_repos:
        print(f"    {repo:45s} {cnt}")

    # Method frequency (how many repos implement each)
    method_freq = defaultdict(int)
    for r in rows:
        method_freq[r["method_name"]] += 1
    top_methods = sorted(method_freq.items(), key=lambda x: -x[1])[:20]
    print("\n  Most common methods (repos implementing):")
    for method, cnt in top_methods:
        print(f"    {method:30s} {cnt} repos")


def main():
    save = "--save" in sys.argv

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = build_matrix(conn)
        print_stats(rows)

        if save:
            save_matrix(conn, rows)
            print(f"\n  Saved {len(rows)} entries to method_matrix table.")
        else:
            print("\n  Dry run — use --save to persist.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
