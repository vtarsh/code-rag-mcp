#!/usr/bin/env python3
"""
Semantic gap scorer: cross-validates task_gaps against vector search results.

For each task in task_history, searches the MCP daemon with the task's
summary + description to find semantically related repos. Then marks each
gap in task_gaps as semantic_match (1) or semantic_miss (0).

Usage:
    python3 scripts/semantic_gap_scorer.py                    # print stats only
    python3 scripts/semantic_gap_scorer.py --save             # save to DB
    python3 scripts/semantic_gap_scorer.py --ticket=PI-54     # single task
"""

import json
import os
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"
DAEMON_URL = "http://localhost:8742"


# ---------------------------------------------------------------------------
# MCP daemon helper
# ---------------------------------------------------------------------------


def mcp_search(query: str, limit: int = 30) -> str:
    """Call MCP search via daemon HTTP API, excluding gotchas and task chunks."""
    data = json.dumps(
        {
            "query": query,
            "limit": limit,
            "exclude_file_types": "gotchas,task",
        }
    ).encode()
    req = urllib.request.Request(
        f"{DAEMON_URL}/tool/search",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result.get("result", "")
    except Exception as e:
        print(f"  [warn] MCP search failed: {e}")
        return ""


# ---------------------------------------------------------------------------
# Repo extraction from search results
# ---------------------------------------------------------------------------


def extract_repos_from_search(text: str) -> set[str]:
    """Extract repo names from MCP search result text (bold **repo-name** patterns)."""
    repos: set[str] = set()
    for m in re.finditer(r"\*\*([a-z][a-z0-9-]+(?:-[a-z0-9]+)*)\*\*", text):
        candidate = m.group(1)
        if len(candidate) > 3 and not candidate.startswith(("the-", "and-", "for-", "not-")):
            repos.add(candidate)
    return repos


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def build_task_query(summary: str, description: str) -> str:
    """Build search query from task summary + first 200 chars of description."""
    query = summary or ""
    if description:
        desc_snippet = description[:200].strip()
        if desc_snippet:
            query = f"{query} {desc_snippet}"
    return query.strip()


def score_task(
    conn: sqlite3.Connection,
    ticket_id: str,
    *,
    save: bool = False,
    verbose: bool = True,
) -> dict:
    """Score all gaps for a single task. Returns stats dict."""
    conn.row_factory = sqlite3.Row

    task = conn.execute(
        "SELECT ticket_id, summary, description FROM task_history WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    if not task:
        if verbose:
            print(f"  [skip] {ticket_id} not found in task_history")
        return {"scored": 0, "matches": 0, "misses": 0}

    gaps = conn.execute(
        "SELECT id, expected FROM task_gaps WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchall()
    if not gaps:
        return {"scored": 0, "matches": 0, "misses": 0}

    # Build query and search
    query = build_task_query(task["summary"] or "", task["description"] or "")
    if not query:
        if verbose:
            print(f"  [skip] {ticket_id} has no summary/description")
        return {"scored": 0, "matches": 0, "misses": 0}

    if verbose:
        print(f"  [{ticket_id}] {task['summary']}")
        print(f"    Searching with {len(query)} char query, {len(gaps)} gaps to score...")

    search_result = mcp_search(query, limit=30)
    semantic_repos = extract_repos_from_search(search_result)

    if verbose:
        print(f"    Semantic repos found: {len(semantic_repos)}")

    matches = 0
    misses = 0
    for gap in gaps:
        gap_repo = gap["expected"]
        is_match = 1 if gap_repo in semantic_repos else 0
        if is_match:
            matches += 1
        else:
            misses += 1

        if save:
            conn.execute(
                "UPDATE task_gaps SET semantic_match = ? WHERE id = ?",
                (is_match, gap["id"]),
            )

    if save:
        conn.commit()

    if verbose:
        print(
            f"    Results: {matches} match, {misses} miss ({matches}/{matches + misses} = {matches / (matches + misses) * 100:.0f}%)"
        )

    return {"scored": matches + misses, "matches": matches, "misses": misses}


def ensure_column(conn: sqlite3.Connection) -> None:
    """Add semantic_match column to task_gaps if it doesn't exist."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task_gaps)").fetchall()}
    if "semantic_match" not in cols:
        conn.execute("ALTER TABLE task_gaps ADD COLUMN semantic_match INTEGER DEFAULT NULL")
        conn.commit()
        print("  Added semantic_match column to task_gaps")


def print_analysis(conn: sqlite3.Connection) -> None:
    """Print precision comparison: semantic_match gaps vs non-match."""
    print(f"\n{'=' * 65}")
    print("SEMANTIC GAP SCORER — ANALYSIS")
    print(f"{'=' * 65}")

    total = conn.execute("SELECT COUNT(*) FROM task_gaps WHERE semantic_match IS NOT NULL").fetchone()[0]
    if total == 0:
        print("  No scored gaps found. Run with --save first.")
        return

    matches = conn.execute("SELECT COUNT(*) FROM task_gaps WHERE semantic_match = 1").fetchone()[0]
    misses = conn.execute("SELECT COUNT(*) FROM task_gaps WHERE semantic_match = 0").fetchone()[0]
    unscored = conn.execute("SELECT COUNT(*) FROM task_gaps WHERE semantic_match IS NULL").fetchone()[0]

    print(f"\n  Total gaps scored:      {total}")
    print(f"  Unscored gaps:          {unscored}")
    print(f"  Semantic match:         {matches} ({matches / total * 100:.1f}%)")
    print(f"  Semantic miss:          {misses} ({misses / total * 100:.1f}%)")

    # Precision comparison using validation column
    print(f"\n  {'─' * 55}")
    print("  PRECISION BY VALIDATION STATUS")
    print(f"  {'─' * 55}")

    for label, sem_val in [("semantic_match", 1), ("semantic_miss", 0)]:
        row = conn.execute(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN validation = 'potential_miss' THEN 1 ELSE 0 END) as tp,
                SUM(CASE WHEN validation = 'related_work' THEN 1 ELSE 0 END) as fp,
                SUM(CASE WHEN validation IS NULL THEN 1 ELSE 0 END) as unknown
            FROM task_gaps WHERE semantic_match = ?""",
            (sem_val,),
        ).fetchone()
        t, tp, fp, unknown = row
        if t == 0:
            print(f"\n  {label}: no gaps")
            continue
        validated = tp + fp
        precision = tp / validated * 100 if validated > 0 else 0
        print(f"\n  {label} (n={t}):")
        print(f"    potential_miss (TP): {tp}")
        print(f"    related_work  (FP): {fp}")
        print(f"    unvalidated:        {unknown}")
        if validated > 0:
            print(f"    precision:          {precision:.1f}% ({tp}/{validated})")

    # Confidence distribution
    print(f"\n  {'─' * 55}")
    print("  CONFIDENCE DISTRIBUTION")
    print(f"  {'─' * 55}")

    for label, sem_val in [("semantic_match", 1), ("semantic_miss", 0)]:
        row = conn.execute(
            "SELECT AVG(confidence), MIN(confidence), MAX(confidence) FROM task_gaps WHERE semantic_match = ?",
            (sem_val,),
        ).fetchone()
        avg_c, min_c, max_c = row
        if avg_c is not None:
            print(f"  {label}: avg={avg_c:.2f}  min={min_c:.2f}  max={max_c:.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    save = "--save" in sys.argv
    ticket = None
    for arg in sys.argv[1:]:
        if arg.startswith("--ticket="):
            ticket = arg.split("=", 1)[1].upper().strip()

    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_column(conn)

        if ticket:
            tickets = [ticket]
        else:
            tickets = [r[0] for r in conn.execute("SELECT ticket_id FROM task_history ORDER BY ticket_id").fetchall()]

        print(f"Scoring {len(tickets)} task(s), save={'yes' if save else 'no'}")
        print(f"{'─' * 65}")

        totals = {"scored": 0, "matches": 0, "misses": 0}
        for t in tickets:
            stats = score_task(conn, t, save=save, verbose=True)
            totals["scored"] += stats["scored"]
            totals["matches"] += stats["matches"]
            totals["misses"] += stats["misses"]

        print(f"\n{'─' * 65}")
        print(
            f"TOTALS: {totals['scored']} gaps scored, "
            f"{totals['matches']} match ({totals['matches'] / totals['scored'] * 100:.1f}%), "
            f"{totals['misses']} miss ({totals['misses'] / totals['scored'] * 100:.1f}%)"
            if totals["scored"] > 0
            else "No gaps to score."
        )

        # Always print analysis if we have scored data (from this run or previous)
        print_analysis(conn)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
