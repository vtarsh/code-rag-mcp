#!/usr/bin/env python3
"""Build vector embeddings for all chunks and store in LanceDB.

Supports multiple embedding models via --model flag.
Uses adaptive batching: short chunks in batches, long chunks one-by-one.
Checkpoints every 2000 chunks for resumable builds.

Usage:
    python3 build_vectors.py                     # default model (coderank)
    python3 build_vectors.py --model minilm      # use MiniLM model
    python3 build_vectors.py --model coderank    # use CodeRankEmbed model
    python3 build_vectors.py --force             # re-embed everything
    python3 build_vectors.py --repos=a,b,c       # incremental (specific repos)
"""

import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import lancedb
from sentence_transformers import SentenceTransformer

# --- Resolve paths from environment or defaults ---
BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = BASE_DIR / "db" / "knowledge.db"
CHECKPOINT_EVERY = 2000

# --- Add project root to path for imports (use script location, not BASE_DIR) ---
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))
from src.models import DEFAULT_MODEL, EMBEDDING_MODELS, get_model_config  # noqa: E402


def parse_args():
    """Parse CLI arguments."""
    model_key = DEFAULT_MODEL
    force = False
    only_repos = None

    for arg in sys.argv[1:]:
        if arg.startswith("--model="):
            model_key = arg.split("=", 1)[1]
        elif arg == "--force":
            force = True
        elif arg.startswith("--repos="):
            only_repos = set(arg.split("=", 1)[1].split(","))
        elif arg == "--list-models":
            print("Available embedding models:")
            for key, m in EMBEDDING_MODELS.items():
                marker = " (default)" if key == DEFAULT_MODEL else ""
                print(f"  {key}{marker}: {m.description}")
            sys.exit(0)

    if model_key not in EMBEDDING_MODELS:
        print(f"Unknown model: {model_key}")
        print(f"Available: {', '.join(EMBEDDING_MODELS.keys())}")
        sys.exit(1)

    return model_key, force, only_repos


def get_all_chunks(conn):
    cursor = conn.execute(
        "SELECT rowid, content, repo_name, file_path, file_type, chunk_type FROM chunks ORDER BY rowid"
    )
    rows = cursor.fetchall()
    print(f"  Loaded {len(rows)} chunks from SQLite")
    return rows


def get_repo_chunks(conn, repo_names):
    placeholders = ",".join("?" for _ in repo_names)
    cursor = conn.execute(
        f"SELECT rowid, content, repo_name, file_path, file_type, chunk_type FROM chunks WHERE repo_name IN ({placeholders})",
        list(repo_names),
    )
    rows = cursor.fetchall()
    print(f"  Loaded {len(rows)} chunks for {len(repo_names)} repos")
    return rows


def prepare_text(content, repo_name, file_type, chunk_type, truncate_at):
    prefix = f"[{repo_name}] [{file_type}/{chunk_type}] "
    return prefix + content[:truncate_at]


def make_record(row, vector):
    rowid, content, repo_name, file_path, file_type, chunk_type = row
    return {
        "rowid": rowid,
        "vector": vector,
        "repo_name": repo_name,
        "file_path": file_path,
        "file_type": file_type,
        "chunk_type": chunk_type,
        "content_preview": content[:300],
    }


def save_checkpoint(checkpoint_path, done_rowids, all_data):
    checkpoint = {"done_rowids": done_rowids, "data": all_data}
    tmp = checkpoint_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(checkpoint, f)
    tmp.rename(checkpoint_path)
    print(f"  [checkpoint] Saved {len(done_rowids)} embeddings to disk", flush=True)


def load_checkpoint(checkpoint_path):
    if not checkpoint_path.exists():
        return set(), []
    try:
        with open(checkpoint_path) as f:
            checkpoint = json.load(f)
        done = set(checkpoint["done_rowids"])
        data = checkpoint["data"]
        print(f"  [checkpoint] Resumed: {len(done)} embeddings already done")
        return done, data
    except Exception as e:
        print(f"  [checkpoint] Failed to load ({e}), starting fresh")
        return set(), []


def embed_adaptive(model, rows, mcfg, checkpoint_path):
    """Embed chunks using adaptive batching with checkpoints."""
    done_rowids, all_data = load_checkpoint(checkpoint_path)

    remaining_rows = [r for r in rows if r[0] not in done_rowids]
    short_rows = [r for r in remaining_rows if len(r[1]) <= mcfg.short_limit]
    long_rows = [r for r in remaining_rows if len(r[1]) > mcfg.short_limit]
    total = len(rows)
    done = len(done_rowids)

    print(f"  Already done: {done}/{total}")
    print(f"  Remaining short (<= {mcfg.short_limit} chars): {len(short_rows)} — batch of {mcfg.batch_size}")
    if mcfg.long_limit > mcfg.short_limit:
        print(
            f"  Remaining long  (> {mcfg.short_limit} chars):  {len(long_rows)} — one by one, up to {mcfg.long_limit} chars"
        )

    if not remaining_rows:
        print("\n  All chunks already embedded!")
        return all_data

    total_start = time.time()
    since_checkpoint = 0

    # Short chunks in batches
    if short_rows:
        print(f"\n  --- Short chunks ({len(short_rows)}) ---")
        for i in range(0, len(short_rows), mcfg.batch_size):
            batch_rows = short_rows[i : i + mcfg.batch_size]
            texts = [prepare_text(r[1], r[2], r[4], r[5], mcfg.short_limit) for r in batch_rows]
            embeddings = model.encode(texts, batch_size=mcfg.batch_size, show_progress_bar=False)

            for idx, row in enumerate(batch_rows):
                all_data.append(make_record(row, embeddings[idx].tolist()))
                done_rowids.add(row[0])

            done += len(batch_rows)
            since_checkpoint += len(batch_rows)
            elapsed = time.time() - total_start
            processed_now = done - (len(rows) - len(remaining_rows))
            rate = processed_now / elapsed if elapsed > 0 else 0
            remaining = total - done
            eta_min = (remaining / rate / 60) if rate > 0 else 0
            print(f"  {done}/{total} ({done * 100 // total}%) — {rate:.1f} emb/s — ETA {eta_min:.0f}min", flush=True)

            if since_checkpoint >= CHECKPOINT_EVERY:
                save_checkpoint(checkpoint_path, list(done_rowids), all_data)
                since_checkpoint = 0

    # Long chunks one by one
    if long_rows and mcfg.long_limit > mcfg.short_limit:
        print(f"\n  --- Long chunks ({len(long_rows)}) ---")
        for row in long_rows:
            text = prepare_text(row[1], row[2], row[4], row[5], mcfg.long_limit)
            embedding = model.encode([text], show_progress_bar=False)
            all_data.append(make_record(row, embedding[0].tolist()))
            done_rowids.add(row[0])

            done += 1
            since_checkpoint += 1
            elapsed = time.time() - total_start
            processed_now = done - (len(rows) - len(remaining_rows))
            rate = processed_now / elapsed if elapsed > 0 else 0
            remaining = total - done
            eta_min = (remaining / rate / 60) if rate > 0 else 0
            print(f"  {done}/{total} ({done * 100 // total}%) — {rate:.1f} emb/s — ETA {eta_min:.0f}min", flush=True)

            if since_checkpoint >= CHECKPOINT_EVERY:
                save_checkpoint(checkpoint_path, list(done_rowids), all_data)
                since_checkpoint = 0

    save_checkpoint(checkpoint_path, list(done_rowids), all_data)
    print(f"\n  Done! {len(all_data)} embeddings in {time.time() - total_start:.1f}s")
    return all_data


def embed_simple(model, rows, mcfg):
    """Embed all chunks in simple batches (for models without long/short split)."""
    all_embeddings = []
    texts = [prepare_text(r[1], r[2], r[4], r[5], mcfg.short_limit) for r in rows]
    start = time.time()

    for i in range(0, len(texts), mcfg.batch_size):
        batch = texts[i : i + mcfg.batch_size]
        embeddings = model.encode(batch, batch_size=mcfg.batch_size, show_progress_bar=False)
        all_embeddings.extend(embeddings.tolist())
        done = min(i + mcfg.batch_size, len(texts))
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(texts) - done) / rate if rate > 0 else 0
        print(f"  {done}/{len(texts)} ({done * 100 // len(texts)}%) — {rate:.0f} emb/s — ETA {eta:.0f}s", end="\r")

    print(f"\n  Done! {len(all_embeddings)} embeddings in {time.time() - start:.1f}s")

    return [make_record(row, all_embeddings[idx]) for idx, row in enumerate(rows)]


def store_vectors(data, lance_path, mcfg):
    """Store vectors in LanceDB and build ANN index."""
    print(f"\n  Storing {len(data)} vectors in LanceDB...")
    start = time.time()

    db = lancedb.connect(str(lance_path))
    with contextlib.suppress(Exception):
        db.drop_table("chunks")

    table = db.create_table("chunks", data=data)
    num_vectors = table.count_rows()
    print(f"  Stored {num_vectors} vectors in {time.time() - start:.1f}s")

    # IVF-PQ index — scale partitions to vector count (K must be < num_vectors)
    if num_vectors < 256:
        # Too few vectors for IVF — skip ANN index, brute-force is fine
        print(f"  Skipping ANN index ({num_vectors} vectors < 256 — brute-force search is fine)")
    else:
        num_partitions = min(64 if mcfg.dim >= 512 else 32, num_vectors // 4)
        num_sub_vectors = min(48 if mcfg.dim >= 512 else 16, mcfg.dim)

        print(f"  Building ANN index ({num_partitions} partitions, {num_sub_vectors} sub-vectors)...")
        start = time.time()
        table.create_index(metric="cosine", num_partitions=num_partitions, num_sub_vectors=num_sub_vectors)
        print(f"  Index built in {time.time() - start:.1f}s")

    lance_size = sum(f.stat().st_size for f in lance_path.rglob("*") if f.is_file()) / (1024 * 1024)
    print(f"\n{'=' * 60}")
    print(f"Vector store: {lance_path}")
    print(f"Total vectors: {table.count_rows()}")
    print(f"Dimensions: {mcfg.dim}")
    print(f"Model: {mcfg.name} ({mcfg.key})")
    print(f"Size on disk: {lance_size:.1f}MB")
    print(f"{'=' * 60}")

    return table


def main():
    model_key, force, only_repos = parse_args()
    mcfg = get_model_config(model_key)
    lance_path = BASE_DIR / "db" / mcfg.lance_dir
    checkpoint_path = BASE_DIR / "db" / f"{mcfg.key}_checkpoint.json"

    print("=" * 60)
    print(f"Building Vector Embeddings — {mcfg.key}")
    print(f"Model: {mcfg.name} ({mcfg.dim}d)")
    print(f"Output: {lance_path}")
    if only_repos:
        print(f"Mode: incremental ({len(only_repos)} repos)")
    print("=" * 60)

    # Load model
    print(f"\n[1/4] Loading {mcfg.key} model...")
    start = time.time()
    model = SentenceTransformer(mcfg.name, trust_remote_code=mcfg.trust_remote_code)
    print(f"  Model loaded in {time.time() - start:.1f}s")

    # Read chunks
    print("\n[2/4] Reading chunks from SQLite...")
    conn = sqlite3.connect(str(DB_PATH))

    # Incremental mode
    if only_repos and lance_path.exists() and not force:
        rows = get_repo_chunks(conn, only_repos)
        if not rows:
            print("  No chunks to embed. Skipping.")
            conn.close()
            return

        # Use simple embedding for incremental (no checkpoint needed)
        print(f"\n[3/4] Embedding {len(rows)} chunks for changed repos...")
        data = embed_simple(model, rows, mcfg)

        # Update LanceDB
        print(f"\n[4/4] Updating LanceDB at {lance_path}...")
        # Validate repo names to prevent injection (only lowercase alphanumeric + hyphens)
        _valid_repo = re.compile(r"^[a-z0-9][a-z0-9-]*$")
        for r in only_repos:
            if not _valid_repo.match(r):
                print(f"  ERROR: Invalid repo name '{r}' — skipping vector update")
                conn.close()
                return
        db = lancedb.connect(str(lance_path))
        table = db.open_table("chunks")
        repo_filter = " OR ".join(f"repo_name = '{r}'" for r in only_repos)
        table.delete(repo_filter)
        print(f"  Deleted old vectors for: {', '.join(sorted(only_repos))}")
        table.add(data)
        print(f"  Added {len(data)} new vectors")

        total_vectors = table.count_rows()
        if total_vectors < 256:
            print(f"  Skipping ANN index ({total_vectors} vectors < 256 — brute-force search is fine)")
        else:
            num_partitions = min(64 if mcfg.dim >= 512 else 32, total_vectors // 4)
            num_sub_vectors = min(48 if mcfg.dim >= 512 else 16, mcfg.dim)
            print("  Rebuilding ANN index...")
            start = time.time()
            table.create_index(
                metric="cosine", num_partitions=num_partitions, num_sub_vectors=num_sub_vectors, replace=True
            )
            print(f"  Index rebuilt in {time.time() - start:.1f}s")
        conn.close()
        return

    # Full build
    rows = get_all_chunks(conn)

    if lance_path.exists() and not force:
        try:
            db = lancedb.connect(str(lance_path))
            existing = db.open_table("chunks")
            if existing.count_rows() == len(rows):
                print(f"\n  LanceDB already has {existing.count_rows()} vectors. Use --force to rebuild.")
                conn.close()
                return
            print(f"\n  LanceDB has {existing.count_rows()} vectors, SQLite has {len(rows)} chunks. Rebuilding...")
        except Exception:
            print("\n  LanceDB exists but can't read. Rebuilding...")

    # Use adaptive batching for models with long_limit > short_limit
    print(f"\n[3/4] Embedding {len(rows)} chunks...")
    if mcfg.long_limit > mcfg.short_limit:
        data = embed_adaptive(model, rows, mcfg, checkpoint_path)
    else:
        data = embed_simple(model, rows, mcfg)

    # Store
    print(f"\n[4/4] Storing in LanceDB at {lance_path}...")
    store_vectors(data, lance_path, mcfg)

    # Cleanup checkpoint
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        print("Checkpoint file removed.")

    conn.close()


if __name__ == "__main__":
    main()
