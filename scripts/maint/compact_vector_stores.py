#!/usr/bin/env python3
"""Compact LanceDB vector stores + prune old versions — keeps them from bloating.

ROOT CAUSE this fixes: every incremental run does append + delete on the LanceDB
vector tables but never pruned old versions/fragments. Over weeks this accumulated
to 22,709 uncompacted versions = 79 GB on disk for ~260 MB of real vectors. The
running daemon mmaps that file, so its *virtual* size balloons to 12-20 GB (even
though resident RAM stays ~hundreds of MB) and faulting those pages in on search
thrashes the page cache on a 16 GB machine.

Run as a [post] step in full_update.sh after every build (daemon is already paused
by build_vectors at that point, so compaction has no concurrent reader). Idempotent
and fast on an already-compact store.

Env: CODE_RAG_HOME (db lives at $CODE_RAG_HOME/db).
"""

from __future__ import annotations

import contextlib
import glob
import os
from datetime import timedelta

import lance

BASE = os.environ.get("CODE_RAG_HOME", os.path.expanduser("~/.code-rag"))
DB_DIR = os.path.join(BASE, "db")
# Vector stores are fully rebuildable from knowledge.db, so we don't need
# version history. Keep only the just-compacted version; prune everything older.
KEEP_OLDER_THAN = timedelta(minutes=1)


def _du_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, f))
    return total / 1024 / 1024


def compact_dataset(ds_path: str) -> str:
    before_mb = _du_mb(ds_path)
    ds = lance.dataset(ds_path)
    before_v = len(ds.versions())
    ds.optimize.compact_files()
    ds.cleanup_old_versions(older_than=KEEP_OLDER_THAN)
    after_v = len(lance.dataset(ds_path).versions())
    after_mb = _du_mb(ds_path)
    rel = os.path.relpath(ds_path, DB_DIR)
    return f"  {rel}: versions {before_v}->{after_v}, size {before_mb:.0f}MB->{after_mb:.0f}MB"


def main() -> int:
    if not os.path.isdir(DB_DIR):
        print(f"compact_vector_stores: no db dir at {DB_DIR}")
        return 0
    datasets = []
    for store in sorted(glob.glob(os.path.join(DB_DIR, "vectors.lance.*"))):
        if not os.path.isdir(store):
            continue
        datasets.extend(sorted(glob.glob(os.path.join(store, "*.lance"))))
    if not datasets:
        print("compact_vector_stores: no LanceDB vector datasets found")
        return 0
    print(f"compact_vector_stores: {len(datasets)} dataset(s)")
    for ds_path in datasets:
        try:
            print(compact_dataset(ds_path))
        except Exception as e:  # never fail the pipeline on a maintenance step
            print(f"  skip {os.path.relpath(ds_path, DB_DIR)}: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
