#!/usr/bin/env python3
"""Sync LanceDB vectors with SQLite chunks by rowid.

Full reconciliation:
  1. Embed chunks missing from LanceDB (SQLite has → LanceDB needs)
  2. Delete orphan vectors (LanceDB has → SQLite no longer has)
  3. Rebuild ANN index if row count changed

Ensures chunks == vectors post-run. Safe to run after any incremental
re-index that touched SQLite chunks (build_index.py --incremental).

Usage:
    CODE_RAG_HOME=~/.code-rag-mcp ACTIVE_PROFILE=pay-com \
        python3.12 scripts/embed_missing_vectors.py [--model=gemini|coderank]
"""

import gc
import os
import sqlite3
import sys
import time
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))

from scripts.build_vectors import embed_simple  # noqa: E402
from src.models import get_model_config  # noqa: E402

import lancedb  # noqa: E402


def parse_args() -> str:
    model_key = "gemini"
    for arg in sys.argv[1:]:
        if arg.startswith("--model="):
            model_key = arg.split("=", 1)[1]
    return model_key


def load_model(model_key: str):
    mcfg = get_model_config(model_key)
    if mcfg.key == "gemini":
        from src.config import GEMINI_API_KEY
        from src.embedding_provider import GeminiEmbeddingProvider

        if not GEMINI_API_KEY:
            print("ERROR: GEMINI_API_KEY not set", file=sys.stderr)
            sys.exit(1)
        return GeminiEmbeddingProvider(GEMINI_API_KEY, dim=mcfg.dim), mcfg

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
    model_key = parse_args()
    db_path = _BASE_DIR / "db" / "knowledge.db"
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    # Resolve LanceDB path without loading the model — model load is expensive
    # (~400 MB for CodeRankEmbed, ~1 GB RAM) and we want to short-circuit when
    # nothing to embed. Metadata lookup is cheap.
    mcfg = get_model_config(model_key)
    lance_path = _BASE_DIR / "db" / mcfg.lance_dir
    if not lance_path.exists():
        print(f"ERROR: {lance_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"[1/6] Reading SQLite chunk rowids (content deferred until embed needed)...")
    conn = sqlite3.connect(str(db_path))
    sqlite_rowid_set = {r[0] for r in conn.execute("SELECT rowid FROM chunks").fetchall()}
    conn.close()
    print(f"  {len(sqlite_rowid_set)} chunks total in SQLite")

    print(f"\n[2/6] Reading existing vectors from {lance_path}...")
    db = lancedb.connect(str(lance_path))
    table = db.open_table("chunks")
    # Read ONLY the rowid column — full to_pandas() pulls all 768-float vectors
    # into memory (~140 MB for 47k rows) just to build a set of integer rowids.
    # Projection via underlying lance dataset is ~10-30× faster + bounded RAM.
    existing_rowids = set(
        table.to_lance().to_table(columns=["rowid"])["rowid"].to_pylist()
    )
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
        # Lazy-load model only when embedding is actually needed — saves
        # 1 GB RAM + model-load time on nights when nothing changed.
        print(f"\n[3/6] Loading {model_key} embedding model...")
        start = time.time()
        model, _mcfg = load_model(model_key)
        print(f"  Loaded in {time.time() - start:.1f}s")

        # Batch SQL IN (SQLite default limit ~999) AND batch embed itself so
        # memory stays bounded — each batch: fetch content, embed, append.
        # Checkpointing is implicit: if embedding dies mid-way, LanceDB keeps
        # what was already appended; next run picks up remaining delta.
        # Try to get MPS cache clearer if available — release GPU buffers after
        # each batch so they don't accumulate across the embed loop.
        try:
            import torch
            _mps_empty = (
                torch.mps.empty_cache
                if torch.backends.mps.is_available() else None
            )
        except Exception:
            _mps_empty = None

        SQL_BATCH = 250
        # LanceDB accumulates a new fragment file per `add()` — by 5-10 fragments
        # the metadata scan on every subsequent add noticeably slows down. We
        # compact every N batches (COMPACT_EVERY) to keep fragment count small.
        # Tuned empirically on M1 Pro / MPS (2026-04-18): 250 × 4 = 1000 chunks
        # per compact gave the best rate (~12-13 emb/s stable) vs 500/100 batch
        # or 500-chunk compact window.
        COMPACT_EVERY = 4  # ~1000 chunks per compact (250 × 4)

        print(f"\n[4/6] Embedding {len(missing_rowids)} missing chunks "
              f"(batches of {SQL_BATCH}, compact every {COMPACT_EVERY} batches)...")
        conn = sqlite3.connect(str(db_path))
        done = 0
        embed_start = time.time()
        batches_since_compact = 0
        for i in range(0, len(missing_rowids), SQL_BATCH):
            batch_ids = missing_rowids[i:i + SQL_BATCH]
            placeholders = ",".join("?" * len(batch_ids))
            batch_rows = conn.execute(
                f"SELECT rowid, content, repo_name, file_path, file_type, chunk_type "
                f"FROM chunks WHERE rowid IN ({placeholders}) ORDER BY rowid",
                batch_ids,
            ).fetchall()
            data = embed_simple(model, batch_rows, mcfg)
            table.add(data)
            done += len(batch_rows)

            # --- aggressive cleanup to stop RAM creep + fragment accumulation ---
            del batch_rows, data
            gc.collect()
            if _mps_empty is not None:
                try:
                    _mps_empty()
                except Exception:
                    pass
            batches_since_compact += 1
            if batches_since_compact >= COMPACT_EVERY:
                compact_start = time.time()
                try:
                    table.optimize()
                except Exception as e:
                    print(f"  [compact failed: {e}]")
                else:
                    print(f"  [compact ok in {time.time() - compact_start:.1f}s]")
                batches_since_compact = 0

            rate = done / max(time.time() - embed_start, 0.1)
            remaining = (len(missing_rowids) - done) / max(rate, 0.01)
            print(f"  {done}/{len(missing_rowids)} ({rate:.1f} emb/s, ~{remaining/60:.0f}m remaining)")
        conn.close()
        # Final compact to leave the table clean
        try:
            table.optimize()
        except Exception:
            pass
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
