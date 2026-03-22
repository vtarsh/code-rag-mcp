#!/usr/bin/env python3
"""Build test-to-source file mapping from chunks data.

Queries test files from the chunks FTS5 table, derives probable source
file paths, optionally verifies they exist, and saves to test_source_map table.

Usage:
    python3 scripts/build_test_map.py          # print stats only
    python3 scripts/build_test_map.py --save   # save to DB
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag")) / "db" / "knowledge.db"

# Fallback for pay-knowledge layout
if not DB_PATH.exists():
    alt = Path.home() / ".pay-knowledge" / "db" / "knowledge.db"
    if alt.exists():
        DB_PATH = alt

# Prefixes to strip (order matters: longer first)
TEST_DIR_PREFIXES = [
    "tests/integration/",
    "tests/unit/",
    "tests/",
    "__tests__/integration/",
    "__tests__/unit/",
    "__tests__/",
]

# Regex: strip .spec or .test before the file extension
SPEC_RE = re.compile(r"\.(spec|test)(\.\w+)$")


def derive_source_path(test_path: str) -> str | None:
    """Derive the probable source file path from a test file path.

    Returns None if the file doesn't look like a real test (e.g. CI yaml,
    proto test fixtures, cypress tests).
    """
    # Skip non-test files that happen to match (CI configs, proto fixtures, cypress)
    if test_path.startswith("ci/") or test_path.startswith("proto/"):
        return None
    if "/cypress/" in test_path:
        return None
    if "fixtures/" in test_path:
        return None

    # Must have .spec. or .test. in the filename
    basename = test_path.rsplit("/", 1)[-1] if "/" in test_path else test_path
    if ".spec." not in basename and ".test." not in basename:
        return None

    result = test_path

    # Handle nested __tests__ dirs (e.g. "workflows/workflows/x/libs/__tests__/foo.spec.js")
    # Replace __tests__/ segment wherever it appears
    result = re.sub(r"/__tests__/(?:unit/|integration/)?", "/", result)

    # Handle top-level test directory prefixes
    # Some paths start with "workflows/tests/unit/..." -> strip "tests/unit/" part
    for prefix in TEST_DIR_PREFIXES:
        # Check if prefix appears after an optional leading segment
        # e.g. "workflows/tests/unit/methods/foo.spec.js"
        idx = result.find(prefix)
        if idx >= 0:
            result = result[:idx] + result[idx + len(prefix) :]
            break

    # Strip .spec or .test from filename
    result = SPEC_RE.sub(r"\2", result)

    return result if result != test_path else None


def get_test_files(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Get distinct (repo_name, file_path) pairs for test files."""
    query = """
        SELECT DISTINCT repo_name, file_path FROM chunks
        WHERE file_path MATCH '"spec" OR "test" OR "__tests__"'
    """
    rows = conn.execute(query).fetchall()
    # FTS5 MATCH is broad; filter precisely in Python
    result = []
    seen = set()
    for repo, fpath in rows:
        if (repo, fpath) in seen:
            continue
        seen.add((repo, fpath))
        if ".spec." in fpath or ".test." in fpath or "/__tests__/" in fpath or fpath.startswith("__tests__/"):
            result.append((repo, fpath))
    return result


def source_exists(conn: sqlite3.Connection, repo: str, source_path: str) -> bool:
    """Check if the derived source file exists in chunks for the same repo."""
    query = """
        SELECT COUNT(*) FROM chunks
        WHERE repo_name = ? AND file_path = ?
    """
    count = conn.execute(query, (repo, source_path)).fetchone()[0]
    return count > 0


def create_table(conn: sqlite3.Connection):
    """Create the test_source_map table (regular, not FTS5)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS test_source_map (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_name TEXT,
            test_file TEXT,
            source_file TEXT,
            verified INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_test_source_map_unique
        ON test_source_map(repo_name, test_file)
    """)
    conn.commit()


def save_mappings(conn: sqlite3.Connection, mappings: list[dict]):
    """Insert or replace mappings into test_source_map."""
    conn.execute("DELETE FROM test_source_map")
    conn.executemany(
        """INSERT INTO test_source_map (repo_name, test_file, source_file, verified)
           VALUES (:repo, :test_file, :source_file, :verified)""",
        mappings,
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Build test-to-source file mapping")
    parser.add_argument("--save", action="store_true", help="Save mappings to DB")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(str(DB_PATH))
    print(f"Using DB: {DB_PATH}")

    # Get all test files
    test_files = get_test_files(conn)
    print(f"Found {len(test_files)} distinct test files")

    # Derive mappings
    mappings = []
    skipped = 0
    for repo, test_path in test_files:
        source = derive_source_path(test_path)
        if source is None:
            skipped += 1
            continue
        verified = 1 if source_exists(conn, repo, source) else 0
        mappings.append(
            {
                "repo": repo,
                "test_file": test_path,
                "source_file": source,
                "verified": verified,
            }
        )

    # Stats
    total = len(mappings)
    verified_count = sum(1 for m in mappings if m["verified"])
    unverified = total - verified_count
    pct = (verified_count / total * 100) if total else 0

    print("\n--- Stats ---")
    print(f"Total mappings:   {total}")
    print(f"Verified:         {verified_count} ({pct:.1f}%)")
    print(f"Unverified:       {unverified}")
    print(f"Skipped (non-test): {skipped}")

    # Show some examples
    if mappings:
        print("\n--- Sample verified mappings ---")
        for m in [m for m in mappings if m["verified"]][:5]:
            print(f"  {m['repo']}: {m['test_file']}")
            print(f"    -> {m['source_file']}")

        unverified_samples = [m for m in mappings if not m["verified"]][:5]
        if unverified_samples:
            print("\n--- Sample unverified mappings ---")
            for m in unverified_samples:
                print(f"  {m['repo']}: {m['test_file']}")
                print(f"    -> {m['source_file']} (not found in chunks)")

    if args.save:
        create_table(conn)
        save_mappings(conn, mappings)
        print(f"\nSaved {total} mappings to test_source_map table")
    else:
        print("\nDry run. Use --save to persist to DB.")

    conn.close()


if __name__ == "__main__":
    main()
