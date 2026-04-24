#!/usr/bin/env python3
"""Build vector embeddings for all chunks and store in LanceDB.

Supports multiple embedding models via --model flag.
Uses adaptive batching: short chunks in batches, long chunks one-by-one.
Checkpoints every CHECKPOINT_EVERY chunks for resumable builds.

2026-04-24: switched to STREAMING writes (memguard fix). Each batch lands in
LanceDB immediately, then Python refs + MPS cache are released, and a psutil
watchdog hard-exits on memory pressure so the next run resumes from the rowid
checkpoint. Matches the pattern from ``scripts/embed_missing_vectors.py``
(commit 74c0732). Previously the full path accumulated all_data in RAM until
the end, which on a 16 GB Mac led to swap + Jetsam SIGKILL.

Usage:
    python3 build_vectors.py                     # default model (coderank)
    python3 build_vectors.py --model minilm      # use MiniLM model
    python3 build_vectors.py --model coderank    # use CodeRankEmbed model
    python3 build_vectors.py --force             # re-embed everything
    python3 build_vectors.py --repos=a,b,c       # incremental (specific repos)
    python3 build_vectors.py --no-pause-daemon   # skip POST /admin/shutdown
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

# --- Resolve paths from environment or defaults ---
BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = BASE_DIR / "db" / "knowledge.db"
CHECKPOINT_EVERY = 2000
# Run optimize() / memory_pressure() this often even when RSS/avail still ok.
COMPACT_EVERY_BATCHES = 25
_VALID_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# --- Add project root to path for imports (use script location, not BASE_DIR) ---
_SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_DIR))
from src.index.builders import _memguard  # noqa: E402
from src.models import DEFAULT_MODEL, EMBEDDING_MODELS, get_model_config  # noqa: E402


def parse_args():
    """Parse CLI arguments."""
    model_key = DEFAULT_MODEL
    force = False
    only_repos = None
    no_reindex = False
    pause_daemon_flag = True

    for arg in sys.argv[1:]:
        if arg.startswith("--model="):
            model_key = arg.split("=", 1)[1]
        elif arg == "--force":
            force = True
        elif arg.startswith("--repos="):
            only_repos = set(arg.split("=", 1)[1].split(","))
        elif arg == "--no-reindex":
            no_reindex = True
        elif arg == "--no-pause-daemon":
            pause_daemon_flag = False
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

    return model_key, force, only_repos, no_reindex, pause_daemon_flag


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


# ---------------------------- Checkpoint helpers ------------------------------


def load_checkpoint_rowids(checkpoint_path):
    """Return set of already-embedded rowids.

    Accepts the legacy ``{done_rowids: [...], data: [...]}`` format and the new
    streaming ``{done_rowids: [...]}`` format. Cached ``data`` is ignored —
    LanceDB is the source of truth for vectors written.
    """
    if not checkpoint_path.exists():
        return set()
    try:
        with open(checkpoint_path) as f:
            checkpoint = json.load(f)
        done = set(checkpoint.get("done_rowids", []))
        print(f"  [checkpoint] Resumed: {len(done)} rowids already embedded")
        return done
    except Exception as e:
        print(f"  [checkpoint] Failed to load ({e}), starting fresh")
        return set()


def save_checkpoint_rowids(checkpoint_path, done_rowids):
    """Atomically write done rowids (streaming format)."""
    payload = {"done_rowids": sorted(done_rowids)}
    tmp = checkpoint_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    tmp.rename(checkpoint_path)
    print(f"  [checkpoint] Saved {len(done_rowids)} rowids to disk", flush=True)


# Back-compat shim — embed_missing_vectors.py imports embed_simple. Returns
# data list (no streaming) so callers can decide how to write it. The full
# build path uses embed_and_write_streaming() instead.
def _encode(model, texts, mcfg, batch_size=None):
    bs = batch_size or mcfg.batch_size
    embeddings = model.encode(texts, batch_size=bs, show_progress_bar=False)
    return [e.tolist() for e in embeddings]


def embed_simple(model, rows, mcfg):
    """Embed all chunks in simple batches and return data list.

    Kept for back-compat with ``scripts/embed_missing_vectors.py`` which calls
    ``table.add(data)`` per outer batch. Memory is bounded by the caller —
    embed_missing_vectors loops outer batches of MISSING_BATCH_SIZE, so the
    in-memory data list stays small.
    """
    all_data = []
    start = time.time()
    total = len(rows)

    for i in range(0, total, mcfg.batch_size):
        batch_rows = rows[i : i + mcfg.batch_size]
        texts = [prepare_text(r[1], r[2], r[4], r[5], mcfg.short_limit) for r in batch_rows]
        embeddings = _encode(model, texts, mcfg)

        for idx, row in enumerate(batch_rows):
            vec = embeddings[idx] if isinstance(embeddings[idx], list) else embeddings[idx].tolist()
            all_data.append(make_record(row, vec))

        done = min(i + mcfg.batch_size, total)
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta = (total - done) / rate if rate > 0 else 0
        print(f"  {done}/{total} ({done * 100 // total}%) — {rate:.0f} emb/s — ETA {eta:.0f}s", end="\r")

    print(f"\n  Done! {len(all_data)} embeddings in {time.time() - start:.1f}s")
    return all_data


# --------------------- Streaming embed-and-write loop -------------------------


def _progress_line(done, total, start_time, remaining_at_start):
    pct = (done * 100 // total) if total else 0
    elapsed = max(time.time() - start_time, 1e-6)
    processed_now = max(done - (total - remaining_at_start), 0)
    rate = processed_now / elapsed if elapsed > 0 else 0
    eta_min = ((total - done) / rate / 60) if rate > 0 else 0
    return f"{done}/{total} ({pct}%) — {rate:.1f} emb/s — ETA {eta_min:.0f}min"


def embed_and_write_streaming(model, rows, mcfg, writer_fn, optimize_cb, checkpoint_path):
    """Embed ``rows`` and write each batch via ``writer_fn`` immediately.

    Releases Python + MPS memory after every batch and hard-exits on
    psutil-detected pressure. Resumes from ``checkpoint_path`` (rowids only).

    Returns count of rowids embedded this invocation.
    """
    done_rowids = load_checkpoint_rowids(checkpoint_path) if checkpoint_path else set()
    remaining_rows = [r for r in rows if r[0] not in done_rowids]
    short_rows = [r for r in remaining_rows if len(r[1]) <= mcfg.short_limit]
    long_rows = [r for r in remaining_rows if len(r[1]) > mcfg.short_limit]
    total = len(rows)
    done = len(done_rowids)
    remaining_at_start = len(remaining_rows)

    print(f"  Already done: {done}/{total}")
    print(f"  Remaining short (<= {mcfg.short_limit} chars): {len(short_rows)} — batch of {mcfg.batch_size}")
    if mcfg.long_limit > mcfg.short_limit:
        print(
            f"  Remaining long  (> {mcfg.short_limit} chars):  {len(long_rows)} — one by one, up to {mcfg.long_limit} chars"
        )

    if not remaining_rows:
        print("\n  All chunks already embedded!")
        return 0

    limits = _memguard.get_limits()
    total_start = time.time()
    since_checkpoint = 0
    batches_since_compact = 0
    embedded_this_run = 0

    def _maybe_log_and_checkpoint(force_log=False):
        nonlocal since_checkpoint
        if force_log or done % max(mcfg.batch_size, 1) == 0:
            print("  " + _progress_line(done, total, total_start, remaining_at_start), flush=True)
        if since_checkpoint >= CHECKPOINT_EVERY:
            if checkpoint_path:
                save_checkpoint_rowids(checkpoint_path, done_rowids)
            since_checkpoint = 0

    # Short chunks — batched.
    if short_rows:
        print(f"\n  --- Short chunks ({len(short_rows)}) ---")
        for i in range(0, len(short_rows), mcfg.batch_size):
            batch_rows = short_rows[i : i + mcfg.batch_size]
            texts = [prepare_text(r[1], r[2], r[4], r[5], mcfg.short_limit) for r in batch_rows]
            embeddings = _encode(model, texts, mcfg)
            batch_data = []
            for idx, row in enumerate(batch_rows):
                vec = embeddings[idx] if isinstance(embeddings[idx], list) else embeddings[idx].tolist()
                batch_data.append(make_record(row, vec))
                done_rowids.add(row[0])
            writer_fn(batch_data)
            n = len(batch_rows)
            done += n
            embedded_this_run += n
            since_checkpoint += n
            batches_since_compact += 1

            del batch_rows, texts, embeddings, batch_data
            _memguard.free_memory()

            _maybe_log_and_checkpoint()

            if batches_since_compact >= COMPACT_EVERY_BATCHES:
                with contextlib.suppress(Exception):
                    optimize_cb()
                batches_since_compact = 0
            _memguard.check_and_maybe_exit(limits=limits, done=done, total=total, compact_cb=optimize_cb)

    # Long chunks — one by one.
    if long_rows and mcfg.long_limit > mcfg.short_limit:
        print(f"\n  --- Long chunks ({len(long_rows)}) ---")
        for row in long_rows:
            text = prepare_text(row[1], row[2], row[4], row[5], mcfg.long_limit)
            embeddings = _encode(model, [text], mcfg)
            vec = embeddings[0] if isinstance(embeddings[0], list) else embeddings[0].tolist()
            writer_fn([make_record(row, vec)])
            done_rowids.add(row[0])
            done += 1
            embedded_this_run += 1
            since_checkpoint += 1
            batches_since_compact += 1

            del text, embeddings, vec
            _memguard.free_memory()

            _maybe_log_and_checkpoint(force_log=True)

            if batches_since_compact >= COMPACT_EVERY_BATCHES:
                with contextlib.suppress(Exception):
                    optimize_cb()
                batches_since_compact = 0
            _memguard.check_and_maybe_exit(limits=limits, done=done, total=total, compact_cb=optimize_cb)

    if checkpoint_path:
        save_checkpoint_rowids(checkpoint_path, done_rowids)
    print(f"\n  Done! Embedded {embedded_this_run} rows this run in {time.time() - total_start:.1f}s")
    return embedded_this_run


# --------------------------- LanceDB write helpers ----------------------------


def _open_or_create_writer(lance_path, only_repos, force):
    """Open/create the LanceDB target for streaming writes.

    Returns (writer_fn, optimize_cb, get_table). Same shape as the helper in
    src/index/builders/docs_vector_indexer.py — kept here for the code tower
    so build_vectors.py stays standalone.
    """
    lance_path.parent.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lance_path))

    if force:
        with contextlib.suppress(Exception):
            db.drop_table("chunks")

    if only_repos and not force:
        for r in only_repos:
            if not _VALID_REPO_RE.match(r):
                raise RuntimeError(f"invalid repo name '{r}' — aborting vector update")
        try:
            table_ref = db.open_table("chunks")
            repo_filter = " OR ".join(f"repo_name = '{r}'" for r in only_repos)
            table_ref.delete(repo_filter)
            print(f"  Deleted old vectors for: {', '.join(sorted(only_repos))}")
        except Exception:
            pass

    state: dict = {"table": None}

    def writer_fn(batch_data):
        if not batch_data:
            return
        if state["table"] is None:
            try:
                state["table"] = db.open_table("chunks")
                state["table"].add(batch_data)
            except Exception:
                state["table"] = db.create_table("chunks", data=batch_data)
        else:
            state["table"].add(batch_data)

    def optimize_cb():
        if state["table"] is not None:
            with contextlib.suppress(Exception):
                state["table"].optimize()

    def get_table():
        return state["table"]

    return writer_fn, optimize_cb, get_table


def _build_or_replace_index(table, mcfg, replace=False):
    """IVF-PQ index builder shared by full + incremental paths."""
    num_vectors = table.count_rows()
    if num_vectors < 256:
        print(f"  Skipping ANN index ({num_vectors} vectors < 256 — brute-force search is fine)")
        return
    num_partitions = min(64 if mcfg.dim >= 512 else 32, num_vectors // 4)
    num_sub_vectors = min(48 if mcfg.dim >= 512 else 16, mcfg.dim)
    print(
        f"  {'Rebuilding' if replace else 'Building'} ANN index ({num_partitions} partitions, {num_sub_vectors} sub-vectors)..."
    )
    start = time.time()
    table.create_index(
        metric="cosine",
        num_partitions=num_partitions,
        num_sub_vectors=num_sub_vectors,
        replace=replace,
    )
    print(f"  Index {'rebuilt' if replace else 'built'} in {time.time() - start:.1f}s")


def _print_summary(table, lance_path, mcfg):
    lance_size = sum(f.stat().st_size for f in lance_path.rglob("*") if f.is_file()) / (1024 * 1024)
    print(f"\n{'=' * 60}")
    print(f"Vector store: {lance_path}")
    print(f"Total vectors: {table.count_rows()}")
    print(f"Dimensions: {mcfg.dim}")
    print(f"Model: {mcfg.name} ({mcfg.key})")
    print(f"Size on disk: {lance_size:.1f}MB")
    print(f"{'=' * 60}")


def main():
    model_key, force, only_repos, no_reindex, pause_daemon_flag = parse_args()
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

    if pause_daemon_flag:
        _memguard.pause_daemon()

    # Load local SentenceTransformer
    print(f"\n[1/4] Loading {mcfg.key} model...")
    start = time.time()
    import torch
    from sentence_transformers import SentenceTransformer

    if torch.backends.mps.is_available():
        device = "mps"
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    model = SentenceTransformer(mcfg.name, trust_remote_code=mcfg.trust_remote_code, device=device)
    print(f"  Model loaded on {device} in {time.time() - start:.1f}s")

    # Read chunks
    print("\n[2/4] Reading chunks from SQLite...")
    conn = sqlite3.connect(str(DB_PATH))

    # Incremental mode shares the same streaming pipeline as the full path —
    # the only difference is the writer_fn pre-deletes the affected repos.
    if only_repos and lance_path.exists() and not force:
        rows = get_repo_chunks(conn, only_repos)
        if not rows:
            print("  No chunks to embed. Skipping.")
            conn.close()
            return

        print(f"\n[3/4] Streaming {len(rows)} chunks for changed repos into LanceDB...")
        writer_fn, optimize_cb, get_table = _open_or_create_writer(lance_path, only_repos, force=False)
        embed_and_write_streaming(
            model,
            rows,
            mcfg,
            writer_fn,
            optimize_cb,
            checkpoint_path=None,  # incremental rebuilds are short, no checkpoint needed
        )

        print(f"\n[4/4] Finalising LanceDB at {lance_path}...")
        table = get_table()
        if table is not None:
            with contextlib.suppress(Exception):
                table.optimize()
            if no_reindex:
                print(f"  Skipping ANN reindex (--no-reindex). Total vectors: {table.count_rows()}")
            else:
                _build_or_replace_index(table, mcfg, replace=True)
            _print_summary(table, lance_path, mcfg)
        conn.close()
        return

    # Full build
    rows = get_all_chunks(conn)

    if lance_path.exists() and not force:
        try:
            db_existing = lancedb.connect(str(lance_path))
            existing = db_existing.open_table("chunks")
            if existing.count_rows() == len(rows):
                print(f"\n  LanceDB already has {existing.count_rows()} vectors. Use --force to rebuild.")
                conn.close()
                return
            print(f"\n  LanceDB has {existing.count_rows()} vectors, SQLite has {len(rows)} chunks. Rebuilding...")
        except Exception:
            print("\n  LanceDB exists but can't read. Rebuilding...")

    print(f"\n[3/4] Streaming {len(rows)} chunks into LanceDB...")
    writer_fn, optimize_cb, get_table = _open_or_create_writer(lance_path, only_repos=None, force=force)
    embed_and_write_streaming(
        model,
        rows,
        mcfg,
        writer_fn,
        optimize_cb,
        checkpoint_path=checkpoint_path,
    )

    print(f"\n[4/4] Finalising LanceDB at {lance_path}...")
    table = get_table()
    if table is not None:
        with contextlib.suppress(Exception):
            table.optimize()
        if no_reindex:
            print(f"  Skipping ANN index (--no-reindex). Total vectors: {table.count_rows()}")
        else:
            _build_or_replace_index(table, mcfg, replace=False)
        _print_summary(table, lance_path, mcfg)

    # Force-build completes → clear stale checkpoint.
    if checkpoint_path.exists():
        with contextlib.suppress(Exception):
            checkpoint_path.unlink()
            print("Checkpoint file removed.")

    conn.close()


if __name__ == "__main__":
    main()
