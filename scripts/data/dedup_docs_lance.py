#!/usr/bin/env python3
"""Dedup duplicate rowids in db/vectors.lance.docs/chunks.

Opens the docs tower LanceDB table, counts rowids that appear more than once,
prints a summary, and — after interactive y/n confirmation — deletes the
duplicates keeping the NEWEST version per rowid (LanceDB append order).

Runnable as:
    python3 scripts/dedup_docs_lance.py

Background: today's 7-attempt retry loop appended the same rowid range
(40058..40982) repeatedly, leaving 988 unique dup rowids (max 12x) in a
49142-row table. The writer_fn dedup (F3) prevents new dupes; this script
cleans up the historical buildup once.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag-mcp"))
LANCE_DIR = BASE_DIR / "db" / "vectors.lance.docs"
TABLE_NAME = "chunks"


def main() -> int:
    if not LANCE_DIR.exists():
        print(f"  [dedup] LanceDB dir missing: {LANCE_DIR}")
        return 1

    try:
        import lancedb  # noqa: WPS433  — lazy import for test envs without lancedb
    except ImportError as exc:
        print(f"  [dedup] lancedb not installed: {exc}")
        return 2

    db = lancedb.connect(str(LANCE_DIR))
    try:
        table = db.open_table(TABLE_NAME)
    except Exception as exc:
        print(f"  [dedup] could not open table '{TABLE_NAME}' in {LANCE_DIR}: {exc}")
        return 3

    total = table.count_rows()
    arrow_tbl = table.to_lance().to_table()  # full scan incl. rowid + vector
    rowids = arrow_tbl.column("rowid").to_pylist()
    counts = Counter(rowids)
    dup_rowids = sorted(rid for rid, n in counts.items() if n > 1)
    dup_rows = sum(n for n in counts.values() if n > 1)
    dup_excess = sum(n - 1 for n in counts.values() if n > 1)

    print("=" * 60)
    print(f"Lance dir     : {LANCE_DIR}")
    print(f"Total rows    : {total}")
    print(f"Unique rowids : {len(counts)}")
    print(f"Dup rowids    : {len(dup_rowids)} (rows: {dup_rows}, excess: {dup_excess})")
    if dup_rowids:
        top = counts.most_common(10)
        print("Top offenders :")
        for rid, n in top:
            if n > 1:
                print(f"  rowid={rid}  count={n}")
    print("=" * 60)

    if not dup_rowids:
        print("  [dedup] no duplicates to prune. Nothing to do.")
        return 0

    ans = input(f"  Delete {dup_excess} duplicate rows (keep NEWEST per rowid)? [y/N]: ").strip().lower()
    if ans not in {"y", "yes"}:
        print("  [dedup] cancelled by user.")
        return 0

    # LanceDB has no "row position" delete; we rebuild the keep-set in Python
    # by scanning append-order and keeping the LAST occurrence per rowid.
    # Then we delete+reinsert (inside a single add() call) using a filter that
    # matches ONLY the dup rowids, then bulk-add the kept records back.
    #
    # Arrow rows are in physical (append) order, so walking the full scan
    # forward yields the newest occurrence last — that is what we keep.
    records = arrow_tbl.to_pylist()
    keep_by_rowid: dict = {}
    for rec in records:
        keep_by_rowid[int(rec["rowid"])] = rec  # overwrites with newer
    keep_for_dups = [keep_by_rowid[int(rid)] for rid in dup_rowids]

    dup_rowids_sql = ",".join(str(int(r)) for r in dup_rowids)
    delete_filter = f"rowid IN ({dup_rowids_sql})"
    print(f"  [dedup] deleting all rows where {delete_filter[:80]}{'...' if len(delete_filter) > 80 else ''}")
    table.delete(delete_filter)
    print(f"  [dedup] re-inserting {len(keep_for_dups)} kept (newest) records for dup rowids")
    table.add(keep_for_dups)

    new_total = table.count_rows()
    print("=" * 60)
    print(f"Before : {total} rows")
    print(f"After  : {new_total} rows (Δ = -{total - new_total})")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
