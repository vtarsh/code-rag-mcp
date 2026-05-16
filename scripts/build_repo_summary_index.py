#!/usr/bin/env python3
"""Build or rebuild the repo_summaries FTS5 table for soft repo prefilter (EXP2).

Per-repo summaries are built from:
  1. file_type='docs' chunks (README, architecture, guides) — preferred.
  2. If no docs: top-3 chunks by length across all file_types.

Each summary is prefixed with the repo name so queries like "trustly callback"
score the grpc-apm-trustly repo on the name token even when docs don't repeat it.

Usage:
    CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com \
        python3 scripts/build_repo_summary_index.py
"""

from __future__ import annotations

import os
import sqlite3
import sys

# --- bootstrap ---
BASE = os.getenv("CODE_RAG_HOME", os.path.expanduser("~/.code-rag"))
sys.path.insert(0, str(BASE))


def _build(db_path: str) -> tuple[int, int]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("DROP TABLE IF EXISTS repo_summaries")
    conn.execute(
        "CREATE VIRTUAL TABLE repo_summaries USING fts5(repo_name UNINDEXED, summary, tokenize='porter unicode61')"
    )

    # Collect all repos from chunks
    repos = [r[0] for r in conn.execute("SELECT DISTINCT repo_name FROM chunks")]

    inserted = 0
    for repo in repos:
        # Prefer docs chunks
        docs_rows = conn.execute(
            "SELECT content FROM chunks WHERE repo_name = ? AND file_type = 'docs' ORDER BY length(content) DESC",
            (repo,),
        ).fetchall()

        if docs_rows:
            parts = [r[0] for r in docs_rows]
        else:
            # Fallback: top-3 longest chunks of any type
            fallback = conn.execute(
                "SELECT content FROM chunks WHERE repo_name = ? ORDER BY length(content) DESC LIMIT 3",
                (repo,),
            ).fetchall()
            parts = [r[0] for r in fallback]

        body = "\n\n".join(parts)
        summary = f"{repo}\n{body}"[:4000]

        conn.execute(
            "INSERT INTO repo_summaries(repo_name, summary) VALUES (?, ?)",
            (repo, summary),
        )
        inserted += 1

    conn.execute("INSERT INTO repo_summaries(repo_summaries) VALUES ('optimize')")
    conn.commit()
    conn.close()
    return inserted, len(repos)


def main() -> int:
    db_path = os.path.join(BASE, "db", "knowledge.db")
    if not os.path.exists(db_path):
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1

    print(f"Building repo_summaries from {db_path} ...")
    inserted, total = _build(db_path)
    print(f"Inserted {inserted} / {total} repo summaries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
