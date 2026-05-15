#!/usr/bin/env python3
"""Build intra-service require() traces for grpc-apm-* and grpc-providers-* repos.

Parses CommonJS require() calls to build file-level call chains:
  methods/sale.js → libs/map-request.js → libs/statuses-map.js

Stores results in:
  1. `internal_traces` table (structured JSON per method)
  2. `chunks` table as chunk_type='internal_trace' (for FTS search)

Usage:
  python3 scripts/build_internal_traces.py          # build all
  python3 scripts/build_internal_traces.py --repo=grpc-apm-payper  # single repo
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
RAW_DIR = BASE_DIR / "raw"
DB_PATH = BASE_DIR / "db" / "tasks.db"

# Match: require('./foo'), require('../libs'), require('../libs/bar')
REQUIRE_RE = re.compile(r"""require\(\s*['"](\.[^'"]+)['"]\s*\)""")

# Provider repo prefixes to scan
PROVIDER_PREFIXES = ("grpc-apm-", "grpc-providers-")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS internal_traces (
            repo_name TEXT NOT NULL,
            method_name TEXT NOT NULL,
            entry_file TEXT NOT NULL,
            trace_json TEXT NOT NULL,
            PRIMARY KEY (repo_name, method_name)
        )
    """)
    conn.commit()


def parse_requires(file_path: Path) -> list[str]:
    """Extract relative require() paths from a JS file."""
    if not file_path.exists():
        return []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    return REQUIRE_RE.findall(text)


def resolve_require(from_file: Path, require_path: str, repo_dir: Path) -> Path | None:
    """Resolve a relative require path to an actual file."""
    base = (from_file.parent / require_path).resolve()

    # Try exact file
    if base.with_suffix(".js").exists():
        return base.with_suffix(".js").resolve()
    # Try directory with index.js
    if (base / "index.js").exists():
        return (base / "index.js").resolve()
    # Try exact (already has extension)
    if base.exists() and base.is_file():
        return base.resolve()
    return None


def trace_file(
    file_path: Path,
    repo_dir: Path,
    visited: set[str] | None = None,
    depth: int = 0,
    max_depth: int = 5,
) -> dict:
    """Recursively trace require() calls from a file.

    Returns a tree:
    {
        "file": "methods/sale.js",
        "requires": [
            {"file": "libs/map-response.js", "requires": [...]},
            {"file": "@pay-com/fetch", "external": true}
        ]
    }
    """
    if visited is None:
        visited = set()

    rel = str(file_path.resolve().relative_to(repo_dir.resolve()))
    if rel in visited or depth > max_depth:
        return {"file": rel, "circular": True}

    visited.add(rel)
    node: dict = {"file": rel, "requires": []}

    for req_path in parse_requires(file_path):
        resolved = resolve_require(file_path, req_path, repo_dir)
        if resolved and resolved.is_relative_to(repo_dir):
            child = trace_file(resolved, repo_dir, visited.copy(), depth + 1, max_depth)
            node["requires"].append(child)
        else:
            # External package or unresolved
            node["requires"].append({"file": req_path, "external": True})

    return node


def flatten_trace(node: dict, prefix: str = "", depth: int = 0) -> list[str]:
    """Flatten a trace tree into readable lines."""
    lines = []
    indent = "  " * depth
    marker = "→ " if depth > 0 else ""
    label = node["file"]
    if node.get("external"):
        label = f"{label} (external)"
    if node.get("circular"):
        label = f"{label} (circular ref)"
    lines.append(f"{indent}{marker}{label}")
    for child in node.get("requires", []):
        lines.extend(flatten_trace(child, prefix, depth + 1))
    return lines


def build_internal_only_chain(node: dict) -> list[str]:
    """Extract just the internal file chain (no externals), flattened unique."""
    files = []
    _collect_internal(node, files, set())
    return files


def _collect_internal(node: dict, acc: list[str], seen: set[str]) -> None:
    if node.get("external") or node.get("circular"):
        return
    f = node["file"]
    if f not in seen:
        seen.add(f)
        acc.append(f)
    for child in node.get("requires", []):
        _collect_internal(child, acc, seen)


def process_repo(repo_name: str, conn: sqlite3.Connection) -> int:
    """Process a single repo, return number of methods traced."""
    repo_dir = RAW_DIR / repo_name
    methods_dir = repo_dir / "methods"

    if not methods_dir.is_dir():
        return 0

    count = 0
    for method_file in sorted(methods_dir.glob("*.js")):
        if method_file.name == "index.js":
            continue

        method_name = method_file.stem
        trace = trace_file(method_file, repo_dir)
        trace_json = json.dumps(trace, indent=2)
        flat_lines = flatten_trace(trace)
        internal_chain = build_internal_only_chain(trace)

        # Store structured trace
        conn.execute(
            "INSERT OR REPLACE INTO internal_traces(repo_name, method_name, entry_file, trace_json) "
            "VALUES (?, ?, ?, ?)",
            (repo_name, method_name, f"methods/{method_name}.js", trace_json),
        )

        # Store as searchable chunk
        chunk_content = (
            f"[{repo_name}] Internal trace for {method_name}:\n"
            + "\n".join(flat_lines)
            + f"\n\nInternal files touched: {', '.join(internal_chain)}"
        )
        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_content, repo_name, f"methods/{method_name}.js", "internal_trace", "internal_trace", "javascript"),
        )

        count += 1

    return count


def main() -> None:
    repo_filter = ""
    for arg in sys.argv[1:]:
        if arg.startswith("--repo="):
            repo_filter = arg.split("=", 1)[1]

    if not DB_PATH.exists():
        print(f"Error: database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    ensure_tables(conn)

    # Clean old traces
    if repo_filter:
        conn.execute("DELETE FROM internal_traces WHERE repo_name = ?", (repo_filter,))
        conn.execute(
            "DELETE FROM chunks WHERE repo_name = ? AND chunk_type = 'internal_trace'",
            (repo_filter,),
        )
    else:
        conn.execute("DELETE FROM internal_traces")
        conn.execute("DELETE FROM chunks WHERE chunk_type = 'internal_trace'")

    total_methods = 0
    total_repos = 0

    if repo_filter:
        repos = [repo_filter]
    else:
        repos = sorted(
            d.name for d in RAW_DIR.iterdir() if d.is_dir() and any(d.name.startswith(p) for p in PROVIDER_PREFIXES)
        )

    for repo_name in repos:
        count = process_repo(repo_name, conn)
        if count > 0:
            total_repos += 1
            total_methods += count
            print(f"  {repo_name}: {count} methods traced")

    conn.commit()
    conn.close()

    print(f"\nDone: {total_methods} methods across {total_repos} repos")


if __name__ == "__main__":
    main()
