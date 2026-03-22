#!/usr/bin/env python3
"""
Build method_matrix: maps repos → methods they implement.

Data sources (priority order):
1. chunks table — methods/*.js files (excluding spec, index)
2. code_facts table — validation/cross-reference
3. providers-proto — canonical RPC method list

Usage:
    python scripts/build_method_matrix.py          # dry-run, print only
    python scripts/build_method_matrix.py --save   # save to DB
"""

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


def parse_proto_methods(content: str) -> list[str]:
    """Parse RPC method names from proto service definition."""
    return re.findall(r"rpc\s+(\w+)\s*\(", content)


def build_matrix(conn: sqlite3.Connection) -> list[dict]:
    """Build the method matrix from all data sources."""
    # Track unique (repo, method) → best source + file_path
    matrix: dict[tuple[str, str], dict] = {}

    # Source 1: chunks table (highest priority)
    rows = conn.execute(
        "SELECT DISTINCT repo_name, file_path FROM chunks "
        "WHERE file_path LIKE 'methods/%.js' AND file_path NOT LIKE '%.spec.%'"
    ).fetchall()
    for repo_name, file_path in rows:
        method = extract_method_name(file_path)
        if method:
            key = (repo_name, method)
            if key not in matrix:
                matrix[key] = {"source": "chunks", "file_path": file_path}

    # Source 2: code_facts table (fills gaps)
    rows = conn.execute(
        "SELECT DISTINCT repo_name, file_path FROM code_facts WHERE file_path LIKE 'methods/%.js'"
    ).fetchall()
    for repo_name, file_path in rows:
        method = extract_method_name(file_path)
        if method:
            key = (repo_name, method)
            if key not in matrix:
                matrix[key] = {"source": "code_facts", "file_path": file_path}

    # Source 3: proto canonical list — record as virtual entries for providers-proto
    proto_rows = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = 'providers-proto' AND chunk_type = 'proto_service'"
    ).fetchall()
    for (content,) in proto_rows:
        for method in parse_proto_methods(content):
            key = ("providers-proto", method)
            if key not in matrix:
                matrix[key] = {"source": "proto", "file_path": "proto/provider.proto"}

    # Convert to list
    results = []
    for (repo_name, method_name), info in sorted(matrix.items()):
        results.append(
            {
                "repo_name": repo_name,
                "method_name": method_name,
                "source": info["source"],
                "file_path": info["file_path"],
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
            UNIQUE(repo_name, method_name)
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
