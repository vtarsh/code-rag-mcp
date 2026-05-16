"""Sync LanceDB vectors with SQLite chunks by rowid.

Full reconciliation:
  1. Embed chunks missing from LanceDB (SQLite has → LanceDB needs)
  2. Delete orphan vectors (LanceDB has → SQLite no longer has)
  3. Rebuild ANN index if row count changed

Ensures chunks == vectors post-run. Safe to run after any incremental
re-index that touched SQLite chunks (build_index.py --incremental).

Usage:
    CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com \
        python3.12 scripts/embed_missing_vectors.py [--model=coderank|minilm]
"""

import contextlib
import gc
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._common import pause_daemon, setup_paths

_BASE_DIR = setup_paths()

import lancedb
import psutil

from scripts.build_vectors import embed_simple
from src.models import get_model_config

_GIB = 1024**3
RSS_SOFT_LIMIT_BYTES = int(float(os.getenv("CODE_RAG_EMBED_RSS_SOFT_GB", "8")) * _GIB)
RSS_HARD_LIMIT_BYTES = int(float(os.getenv("CODE_RAG_EMBED_RSS_HARD_GB", "10")) * _GIB)
SYS_AVAIL_SOFT_BYTES = int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_SOFT_GB", "2")) * _GIB)
SYS_AVAIL_HARD_BYTES = int(float(os.getenv("CODE_RAG_EMBED_SYS_AVAIL_HARD_GB", "0.8")) * _GIB)
DAEMON_PORT = int(os.getenv("CODE_RAG_DAEMON_PORT", "8742"))


def parse_args() -> tuple[str, bool]:
    model_key = "coderank"
    pause_daemon_flag = True  # default: serialize with daemon to avoid RAM doubling
    for arg in sys.argv[1:]:
        if arg.startswith("--model="):
            model_key = arg.split("=", 1)[1]
        elif arg == "--no-pause-daemon":
            pause_daemon_flag = False
    return model_key, pause_daemon_flag


def load_model(model_key: str):
    mcfg = get_model_config(model_key)

    import torch
    from sentence_transformers import SentenceTransformer

    if torch.backends.mps.is_available():
        device = "mps"
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"
    print(f"  Using device: {device}")

    model = SentenceTransformer(mcfg.name, trust_remote_code=mcfg.trust_remote_code, device=device)
    return model, mcfg


def main() -> None:
    model_key, pause_daemon_flag = parse_args()
    db_path = _BASE_DIR / "db" / "knowledge.db"
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    mcfg = get_model_config(model_key)
    lance_path = _BASE_DIR / "db" / mcfg.lance_dir
    if not lance_path.exists():
        print(f"ERROR: {lance_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print("[1/6] Reading SQLite chunk rowids (content deferred until embed needed)...")
    conn = sqlite3.connect(str(db_path))
    sqlite_rowid_set = {r[0] for r in conn.execute("SELECT rowid FROM chunks").fetchall()}
    conn.close()
    print(f"  {len(sqlite_rowid_set)} chunks total in SQLite")

    print(f"\n[2/6] Reading existing vectors from {lance_path}...")
    db = lancedb.connect(str(lance_path))
    table = db.open_table("chunks")
    existing_rowids = set(table.to_lance().to_table(columns=["rowid"])["rowid"].to_pylist())
    print(f"  {len(existing_rowids)} existing vectors in LanceDB")

    missing_rowids = sorted(sqlite_rowid_set - existing_rowids)
    orphan_rowids = sorted(existing_rowids - sqlite_rowid_set)
    print(f"  {len(missing_rowids)} missing rowids (need embedding)")
    print(f"  {len(orphan_rowids)} orphan rowids (need deletion)")

    if not missing_rowids and not orphan_rowids:
        print("\nNothing to do. Store is already consistent.")
        return

    index_dirty = False

    if missing_rowids:
        if pause_daemon_flag:
            pause_daemon()

        print(f"\n[3/6] Loading {model_key} embedding model...")
        start = time.time()
        model, _mcfg = load_model(model_key)
        print(f"  Loaded in {time.time() - start:.1f}s")

        try:
            import torch

            _mps_empty = torch.mps.empty_cache if torch.backends.mps.is_available() else None
        except Exception:
            _mps_empty = None

        SQL_BATCH = 250
        COMPACT_EVERY = 4  # ~1000 chunks per compact (250 x 4)

        print(
            f"\n[4/6] Embedding {len(missing_rowids)} missing chunks "
            f"(batches of {SQL_BATCH}, compact every {COMPACT_EVERY} batches)..."
        )
        conn = sqlite3.connect(str(db_path))
        done = 0
        embed_start = time.time()
        batches_since_compact = 0
        for i in range(0, len(missing_rowids), SQL_BATCH):
            batch_ids = missing_rowids[i : i + SQL_BATCH]
            placeholders = ",".join("?" * len(batch_ids))
            batch_rows = conn.execute(
                f"SELECT rowid, content, repo_name, file_path, file_type, chunk_type "
                f"FROM chunks WHERE rowid IN ({placeholders}) ORDER BY rowid",
                batch_ids,
            ).fetchall()
            data = embed_simple(model, batch_rows, mcfg)
            table.add(data)
            done += len(batch_rows)

            del batch_rows, data
            gc.collect()
            if _mps_empty is not None:
                with contextlib.suppress(Exception):
                    _mps_empty()
            batches_since_compact += 1
            rss = psutil.Process().memory_info().rss
            available = psutil.virtual_memory().available
            mem_pressure = rss >= RSS_SOFT_LIMIT_BYTES or available <= SYS_AVAIL_SOFT_BYTES
            if batches_since_compact >= COMPACT_EVERY or mem_pressure:
                reason = f"rss={rss / _GIB:.1f}G avail={available / _GIB:.1f}G" if mem_pressure else "scheduled"
                compact_start = time.time()
                try:
                    table.optimize()
                except Exception as e:
                    print(f"  [compact failed ({reason}): {e}]")
                else:
                    print(f"  [compact ok in {time.time() - compact_start:.1f}s ({reason})]")
                batches_since_compact = 0
                if mem_pressure:
                    gc.collect()
                    if _mps_empty is not None:
                        with contextlib.suppress(Exception):
                            _mps_empty()
                    rss_after = psutil.Process().memory_info().rss
                    avail_after = psutil.virtual_memory().available
                    if rss_after >= RSS_HARD_LIMIT_BYTES or avail_after <= SYS_AVAIL_HARD_BYTES:
                        print(
                            f"  [hard memory pressure: rss={rss_after / _GIB:.1f}G "
                            f"avail={avail_after / _GIB:.1f}G; exiting cleanly at "
                            f"{done}/{len(missing_rowids)} — next run resumes from delta]",
                            flush=True,
                        )
                        sys.exit(0)
                    if rss_after >= RSS_SOFT_LIMIT_BYTES or avail_after <= SYS_AVAIL_SOFT_BYTES:
                        print(
                            f"  [rss={rss_after / _GIB:.1f}G avail={avail_after / _GIB:.1f}G "
                            f"still tight after compact; sleeping 30s]",
                            flush=True,
                        )
                        time.sleep(30)

            rate = done / max(time.time() - embed_start, 0.1)
            remaining = (len(missing_rowids) - done) / max(rate, 0.01)
            print(f"  {done}/{len(missing_rowids)} ({rate:.1f} emb/s, ~{remaining / 60:.0f}m remaining)")
        conn.close()
        with contextlib.suppress(Exception):
            table.optimize()
        print(f"\n[5/6] Appended {done} vectors to LanceDB")
        index_dirty = True

    if orphan_rowids:
        print(f"\n[5/6] Deleting {len(orphan_rowids)} orphan vectors in batches of 500...")
        for i in range(0, len(orphan_rowids), 500):
            batch = orphan_rowids[i : i + 500]
            table.delete(f"rowid IN ({','.join(str(r) for r in batch)})")
            if i % 5000 == 0 or i + 500 >= len(orphan_rowids):
                print(f"  {min(i + 500, len(orphan_rowids))}/{len(orphan_rowids)}")
        index_dirty = True

    new_total = table.count_rows()
    print(f"\nLanceDB total: {new_total} vectors")

    if index_dirty and new_total >= 256:
        num_partitions = min(64 if mcfg.dim >= 512 else 32, new_total // 4)
        num_sub_vectors = min(48 if mcfg.dim >= 512 else 16, mcfg.dim)
        print(f"\n[6/6] Rebuilding ANN index ({num_partitions} partitions)...")
        idx_start = time.time()
        table.create_index(
            metric="cosine",
            num_partitions=num_partitions,
            num_sub_vectors=num_sub_vectors,
            replace=True,
        )
        print(f"  Index rebuilt in {time.time() - idx_start:.1f}s")

    print(
        f"\nDone. Added {len(missing_rowids)}, removed {len(orphan_rowids)} "
        f"for {model_key}. Final: {new_total} vectors."
    )


if __name__ == "__main__":
    main()
