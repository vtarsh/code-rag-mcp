"""Shared utilities for benchmark scripts.

Centralizes profile resolution, DB access, and common search wrappers
used by benchmark_queries.py, benchmark_realworld.py, benchmark_flows.py,
and detect_blind_spots.py.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts._common import setup_paths

setup_paths()

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE / "db" / "knowledge.db"


def resolve_profile_dir() -> Path:
    """Resolve the active profile directory (mirrors src/config.py logic)."""
    if env_profile := os.getenv("ACTIVE_PROFILE"):
        return _BASE / "profiles" / env_profile
    marker = _BASE / ".active_profile"
    if marker.exists():
        name = marker.read_text().strip()
        if name and (_BASE / "profiles" / name).is_dir():
            return _BASE / "profiles" / name
    return _BASE / "profiles" / "example"


def get_db() -> sqlite3.Connection:
    """Open a SQLite connection to the knowledge DB."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def run_fts_search(conn: sqlite3.Connection, query: str, limit: int = 20) -> dict:
    """Run FTS5 search, return dict with repos set, results list, and optional error."""
    tokens = query.split()
    sanitized = []
    for t in tokens:
        if len(t) < 3:
            continue
        if "-" in t:
            sanitized.append(f'"{t}"')
        else:
            sanitized.append(t)
    fts_query = " OR ".join(sanitized) if sanitized else query

    try:
        rows = conn.execute(
            """SELECT repo_name, file_path,
                      snippet(chunks, 0, '>>>', '<<<', '...', 30) as snippet
               FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
        return {
            "repos": set(r["repo_name"] for r in rows),
            "results": [(r["repo_name"], r["file_path"], r["snippet"][:200]) for r in rows],
        }
    except sqlite3.OperationalError as e:
        return {"repos": set(), "results": [], "error": str(e)}


def run_hybrid_search(query: str, limit: int = 10) -> dict:
    """Run full hybrid search (FTS + vector + RRF + reranker)."""
    try:
        from src.search.fts import expand_query
        from src.search.hybrid import hybrid_search

        expanded = expand_query(query)
        ranked, err, _total = hybrid_search(expanded, limit=limit)
        return {
            "repos": set(r["repo_name"] for r in ranked),
            "results": [(r["repo_name"], r["file_path"], r.get("snippet", "")[:200]) for r in ranked],
            "error": err,
        }
    except Exception as e:
        return {"repos": set(), "results": [], "error": str(e)}
