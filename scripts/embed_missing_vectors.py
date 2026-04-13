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

    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(mcfg.name, trust_remote_code=mcfg.trust_remote_code)
    return model, mcfg


def main() -> None:
    model_key = parse_args()
    db_path = _BASE_DIR / "db" / "knowledge.db"
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"[1/6] Loading {model_key} embedding model...")
    start = time.time()
    model, mcfg = load_model(model_key)
    print(f"  Loaded in {time.time() - start:.1f}s")

    lance_path = _BASE_DIR / "db" / mcfg.lance_dir
    if not lance_path.exists():
        print(f"ERROR: {lance_path} does not exist", file=sys.stderr)
        sys.exit(1)

    print(f"\n[2/6] Reading SQLite chunks from {db_path}...")
    conn = sqlite3.connect(str(db_path))
    sqlite_rows = conn.execute(
        "SELECT rowid, content, repo_name, file_path, file_type, chunk_type FROM chunks ORDER BY rowid"
    ).fetchall()
    conn.close()
    sqlite_by_rowid = {r[0]: r for r in sqlite_rows}
    sqlite_rowid_set = set(sqlite_by_rowid.keys())
    print(f"  {len(sqlite_by_rowid)} chunks total in SQLite")

    print(f"\n[3/6] Reading existing vectors from {lance_path}...")
    db = lancedb.connect(str(lance_path))
    table = db.open_table("chunks")
    df = table.to_pandas()
    existing_rowids = set(df["rowid"].astype(int).tolist())
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
        print(f"\n[4/6] Embedding {len(missing_rowids)} missing chunks...")
        missing_rows = [sqlite_by_rowid[rid] for rid in missing_rowids]
        data = embed_simple(model, missing_rows, mcfg)
        print(f"\n[5/6] Appending {len(data)} vectors to LanceDB...")
        table.add(data)
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
