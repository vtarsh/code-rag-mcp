"""Docs-tower vector indexer (two-tower migration, 2026-04-23).

Reads doc-flavoured chunks from ``db/knowledge.db`` and writes embeddings to
``db/vectors.lance.docs/`` using the ``nomic-embed-text-v1.5`` model configured
under ``src/models.EMBEDDING_MODELS["docs"]``.

2026-04-24: switched to STREAMING writes (memguard fix). Each batch lands in
LanceDB immediately, then Python refs + MPS cache are released, and a psutil
watchdog hard-exits on memory pressure so the next run resumes cleanly from
the rowid checkpoint. Matches the pattern from
``scripts/embed_missing_vectors.py`` (commit 74c0732) that had prevented the
nightly Jetsam SIGKILLs; the full-build path previously still accumulated
``all_data`` in RAM and leaked MPS buffers across batches.

Interface:
    build_docs_vectors(db_path, lance_dir, *, force=False,
                       checkpoint_path=None, only_repos=None, log_every=500,
                       no_reindex=False, pause_daemon=True)
    -> {"chunks_embedded": int, "vectors_stored": int, "lance_path": str}
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import src.index.builders._memguard as _memguard

DOC_FILE_TYPES: tuple[str, ...] = (
    "doc",
    "docs",
    "gotchas",
    "reference",
    "provider_doc",
    "task",
    "flow_annotation",
    "dictionary",
    "domain_registry",
)

CHECKPOINT_EVERY = 5000
COMPACT_EVERY_BATCHES = 20
_VALID_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

def _ensure_chunks_table(conn: sqlite3.Connection) -> None:
    """Raise a clean RuntimeError if the `chunks` table is missing.

    Without this, callers see a raw sqlite3.OperationalError which is hard to
    act on from orchestrator code.
    """
    row = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name='chunks'").fetchone()
    if row is None:
        raise RuntimeError("knowledge.db has no 'chunks' table — run build_index first")

def fetch_doc_chunks(
    conn: sqlite3.Connection,
    only_repos: set[str] | None = None,
) -> list[tuple]:
    """Return all chunk rows whose file_type is in DOC_FILE_TYPES.

    Exposed at module level so tests can drive it against an in-memory DB.
    """
    _ensure_chunks_table(conn)
    placeholders = ",".join("?" for _ in DOC_FILE_TYPES)
    params: list = list(DOC_FILE_TYPES)
    sql = (
        "SELECT rowid, content, repo_name, file_path, file_type, chunk_type "
        f"FROM chunks WHERE file_type IN ({placeholders})"
    )
    if only_repos:
        repo_placeholders = ",".join("?" for _ in only_repos)
        sql += f" AND repo_name IN ({repo_placeholders})"
        params.extend(sorted(only_repos))
    sql += " ORDER BY rowid"
    return conn.execute(sql, params).fetchall()

def _load_checkpoint(path: Path | None) -> set[int]:
    """Return the set of already-embedded rowids.

    Accepts both the legacy format ``{"done_rowids": [...], "data": [...]}``
    and the streaming format ``{"done_rowids": [...]}``. The cached ``data``
    field is ignored — LanceDB is the source of truth for written vectors.
    """
    if path is None or not path.exists():
        return set()
    try:
        with open(path) as f:
            ck = json.load(f)
        return set(ck.get("done_rowids", []))
    except Exception as e:
        print(f"  [checkpoint] failed to load ({e}); starting fresh")
        return set()

def _save_checkpoint(path: Path | None, done_rowids) -> None:
    """Write done rowids atomically via tmp + rename.

    Streaming mode: we only persist rowids. LanceDB holds the embeddings.
    Older checkpoints with a ``data`` field are still readable (see
    :func:`_load_checkpoint`) but we never write one again.
    """
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {"done_rowids": sorted(done_rowids)}
    with open(tmp, "w") as f:
        json.dump(payload, f)
    tmp.rename(path)
    print(f"  [checkpoint] saved {len(done_rowids)} rowids to {path.name}", flush=True)

def _prepare_text(
    content: str,
    repo_name: str,
    file_type: str,
    chunk_type: str,
    truncate_at: int,
    document_prefix: str,
) -> str:
    """Build the string that gets embedded.

    Layout mirrors scripts/build_vectors.py::prepare_text but prepends the
    model's document_prefix (required by nomic-embed-text-v1.5).
    """
    body = f"[{repo_name}] [{file_type}/{chunk_type}] {content[:truncate_at]}"
    return f"{document_prefix}{body}" if document_prefix else body

def _make_record(row: tuple, vector) -> dict:
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

def _to_list(vec) -> list[float]:
    """Normalize encoder output (np.ndarray or list) to list[float]."""
    if hasattr(vec, "tolist"):
        return vec.tolist()
    return list(vec)

# ------------------------------- Encoding -------------------------------------

def _encode(model, texts: list[str], batch_size: int) -> list[list[float]]:
    raw = model.encode(texts, batch_size=batch_size, show_progress_bar=False)
    return [_to_list(v) for v in raw]

def _progress(done: int, total: int, start_time: float, remaining_at_start: int) -> str:
    pct = (done * 100 // total) if total else 0
    elapsed = max(time.time() - start_time, 1e-6)
    processed_now = max(done - (total - remaining_at_start), 0)
    rate = processed_now / elapsed if elapsed > 0 else 0
    eta_min = ((total - done) / rate / 60) if rate > 0 else 0
    return f"{done}/{total} ({pct}%) — {rate:.1f} emb/s — ETA {eta_min:.0f}min"

# --------------------- Streaming embed-and-write loop -------------------------

def _embed_and_write_streaming(
    model,
    rows: list[tuple],
    mcfg,
    writer_fn,
    optimize_cb,
    checkpoint_path: Path | None,
    log_every: int,
) -> int:
    """Embed ``rows`` and write batches through ``writer_fn`` one at a time.

    Releases Python + MPS memory after every batch and hard-exits on
    psutil-detected pressure so the next run resumes from the on-disk rowid
    checkpoint. ``optimize_cb`` is invoked when pressure goes soft and every
    COMPACT_EVERY_BATCHES as a scheduled fragment cleanup.

    Returns the total count of rowids embedded this invocation (not cumulative
    with any prior runs in the checkpoint).
    """
    done_rowids = _load_checkpoint(checkpoint_path)
    remaining = [r for r in rows if r[0] not in done_rowids]
    short_rows = [r for r in remaining if len(r[1]) <= mcfg.short_limit]
    long_rows = [r for r in remaining if len(r[1]) > mcfg.short_limit]
    total = len(rows)
    done = len(done_rowids)
    remaining_at_start = len(remaining)

    print(f"  Already done: {done}/{total}")
    print(f"  Remaining short (<= {mcfg.short_limit} chars): {len(short_rows)} — batch of {mcfg.batch_size}")
    if mcfg.long_limit > mcfg.short_limit:
        print(
            f"  Remaining long  (> {mcfg.short_limit} chars):  {len(long_rows)} — one by one, up to {mcfg.long_limit} chars"
        )

    if not remaining:
        print("\n  All docs chunks already embedded!")
        return 0

    limits = _memguard.get_limits()
    start = time.time()
    since_checkpoint = 0
    since_log = 0
    batches_since_compact = 0
    embedded_this_run = 0

    def _flush_checkpoint_and_log() -> None:
        nonlocal since_checkpoint, since_log
        if since_log >= log_every:
            print("  " + _progress(done, total, start, remaining_at_start), flush=True)
            since_log = 0
        if since_checkpoint >= CHECKPOINT_EVERY:
            _save_checkpoint(checkpoint_path, done_rowids)
            since_checkpoint = 0

    # Short chunks — batched.
    for i in range(0, len(short_rows), mcfg.batch_size):
        batch_rows = short_rows[i : i + mcfg.batch_size]
        texts = [_prepare_text(r[1], r[2], r[4], r[5], mcfg.short_limit, mcfg.document_prefix) for r in batch_rows]
        vectors = _encode(model, texts, mcfg.batch_size)
        batch_data = [_make_record(row, vec) for row, vec in zip(batch_rows, vectors, strict=True)]
        writer_fn(batch_data)
        for row in batch_rows:
            done_rowids.add(row[0])
        n = len(batch_rows)
        done += n
        embedded_this_run += n
        since_checkpoint += n
        since_log += n
        batches_since_compact += 1

        # Release refs before watchdog inspects RSS.
        del batch_rows, texts, vectors, batch_data
        _memguard.free_memory()

        _flush_checkpoint_and_log()

        if batches_since_compact >= COMPACT_EVERY_BATCHES:
            with contextlib.suppress(Exception):
                optimize_cb()
            batches_since_compact = 0
        _memguard.check_and_maybe_exit(limits=limits, done=done, total=total, compact_cb=optimize_cb)

    # Long chunks — one by one; still respect watchdog / checkpoint cadence.
    if mcfg.long_limit > mcfg.short_limit:
        for row in long_rows:
            text = _prepare_text(row[1], row[2], row[4], row[5], mcfg.long_limit, mcfg.document_prefix)
            vectors = _encode(model, [text], mcfg.batch_size)
            batch_data = [_make_record(row, vectors[0])]
            writer_fn(batch_data)
            done_rowids.add(row[0])
            done += 1
            embedded_this_run += 1
            since_checkpoint += 1
            since_log += 1
            batches_since_compact += 1

            del text, vectors, batch_data
            _memguard.free_memory()

            _flush_checkpoint_and_log()

            if batches_since_compact >= COMPACT_EVERY_BATCHES:
                with contextlib.suppress(Exception):
                    optimize_cb()
                batches_since_compact = 0
            _memguard.check_and_maybe_exit(limits=limits, done=done, total=total, compact_cb=optimize_cb)

    _save_checkpoint(checkpoint_path, done_rowids)
    print(f"\n  Docs tower: embedded {embedded_this_run} rows this run in {time.time() - start:.1f}s")
    return embedded_this_run

# --------------------------- LanceDB write --------------------------------

def _build_ivfpq_index(table, num_vectors: int, dim: int) -> None:
    """IVF-PQ index tuning consistent with scripts/build_vectors.py."""
    if num_vectors < 256:
        print(f"  Skipping ANN index ({num_vectors} vectors < 256 — brute-force search is fine)")
        return
    num_partitions = min(64, num_vectors // 4)
    num_sub_vectors = min(48, dim)
    print(f"  Building ANN index ({num_partitions} partitions, {num_sub_vectors} sub-vectors)...")
    start = time.time()
    table.create_index(
        metric="cosine",
        num_partitions=num_partitions,
        num_sub_vectors=num_sub_vectors,
    )
    print(f"  Index built in {time.time() - start:.1f}s")

def _reindex_with_replace(table, num_vectors: int, dim: int) -> None:
    """Like _build_ivfpq_index but uses replace=True for incremental updates."""
    if num_vectors < 256:
        print(f"  Skipping ANN index ({num_vectors} vectors < 256 — brute-force search is fine)")
        return
    num_partitions = min(64, num_vectors // 4)
    num_sub_vectors = min(48, dim)
    print("  Rebuilding ANN index...")
    start = time.time()
    table.create_index(
        metric="cosine",
        num_partitions=num_partitions,
        num_sub_vectors=num_sub_vectors,
        replace=True,
    )
    print(f"  Index rebuilt in {time.time() - start:.1f}s")

def _open_or_create_writer(
    lance_dir: Path,
    only_repos: set[str] | None,
    force: bool,
):
    """Prepare the LanceDB target for streaming writes.

    Returns ``(writer_fn, optimize_cb, get_table)`` where:
      * ``writer_fn(batch_data: list[dict])`` appends to the table (creates on
        first call if needed).
      * ``optimize_cb()`` runs ``table.optimize()`` when the table exists.
      * ``get_table()`` returns the underlying LanceDB table once created.

    Semantics:
      * ``force``: drop existing table first; next write recreates it.
      * ``only_repos`` on an existing table: delete those repos' rows, then
        append new batches.
      * Otherwise: open the existing table (or create on first batch).
    """
    import lancedb  # deferred for test mocking

    lance_dir.parent.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(lance_dir))

    if force:
        with contextlib.suppress(Exception):
            db.drop_table("chunks")

    # Incremental mode: clean the repos being re-embedded so `add` below
    # does not produce duplicate rowids.
    if only_repos and not force:
        for r in only_repos:
            if not _VALID_REPO_RE.match(r):
                raise RuntimeError(f"invalid repo name '{r}' — aborting docs vector update")
        try:
            table_ref = db.open_table("chunks")
            repo_filter = " OR ".join(f"repo_name = '{r}'" for r in only_repos)
            table_ref.delete(repo_filter)
            print(f"  Deleted old docs vectors for: {', '.join(sorted(only_repos))}")
        except Exception:
            # No existing table; first write will create it.
            pass

    state: dict[str, Any] = {"table": None}

    def writer_fn(batch_data: list[dict]) -> None:
        if not batch_data:
            return
        if state["table"] is None:
            # First write of the run — try to attach to existing table
            # (non-force / non-initial-drop scenarios). Fall through to
            # create_table if attach fails.
            try:
                state["table"] = db.open_table("chunks")
                state["table"].add(batch_data)
            except Exception:
                state["table"] = db.create_table("chunks", data=batch_data)
        else:
            state["table"].add(batch_data)

    def optimize_cb() -> None:
        if state["table"] is not None:
            state["table"].optimize()

    def get_table():
        return state["table"]

    return writer_fn, optimize_cb, get_table

# ------------------------------- Entry point ----------------------------------

def _load_sentence_transformer(cfg):
    """Load the SentenceTransformer for the docs tower on the best device.

    Imported lazily + wrapped in a helper so tests can monkeypatch this
    without dragging torch into the suite.
    """
    import torch
    from sentence_transformers import SentenceTransformer

    if torch.backends.mps.is_available():
        device = "mps"
    elif torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    model = SentenceTransformer(cfg.name, trust_remote_code=cfg.trust_remote_code, device=device)
    return model, device

def build_docs_vectors(
    db_path: Path,
    lance_dir: Path,
    *,
    force: bool = False,
    checkpoint_path: Path | None = None,
    only_repos: set[str] | None = None,
    log_every: int = 500,
    no_reindex: bool = False,
    pause_daemon: bool = True,
) -> dict:
    """Embed all doc-flavoured chunks from ``db_path`` into ``lance_dir``.

    Returns a summary dict: chunks_embedded, vectors_stored, lance_path.

    - ``pause_daemon`` (default True): POST /admin/shutdown to the MCP daemon
      before loading the model so the resident ~1 GB CodeRankEmbed does not
      compete for RAM. launchd respawns the daemon after the build.
    - ``no_reindex``: skip IVF-PQ (re)build — useful when chaining multiple
      incremental runs that share one reindex at the end.
    - The embed loop streams each batch directly into LanceDB and releases
      refs + MPS cache between batches; a psutil watchdog hard-exits on
      memory pressure so the next run resumes from the rowid checkpoint.
    """
    from src.models import get_model_config

    mcfg = get_model_config("docs")
    db_path = Path(db_path)
    lance_dir = Path(lance_dir)
    checkpoint_path = Path(checkpoint_path) if checkpoint_path else None

    print("=" * 60)
    print(f"Docs Tower — {mcfg.key}")
    print(f"Model: {mcfg.name} ({mcfg.dim}d)")
    print(f"Output: {lance_dir}")
    if only_repos:
        print(f"Mode: incremental ({len(only_repos)} repos)")
    print("=" * 60)

    if pause_daemon:
        _memguard.pause_daemon()

    # `--force` semantics: wipe slate. Drop any stale checkpoint NOW so the
    # streaming loop doesn't skip rowids whose embeddings only existed in the
    # about-to-be-dropped LanceDB table.
    if force and checkpoint_path and checkpoint_path.exists():
        with contextlib.suppress(Exception):
            checkpoint_path.unlink()
            print("  Cleared stale checkpoint (force build).")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = fetch_doc_chunks(conn, only_repos=only_repos)
    finally:
        conn.close()

    print(f"  Loaded {len(rows)} doc chunks (file_type IN {DOC_FILE_TYPES})")

    if not rows:
        print("  No doc chunks to embed — nothing to do.")
        return {
            "chunks_embedded": 0,
            "vectors_stored": 0,
            "lance_path": str(lance_dir),
        }

    print(f"\n[1/3] Loading {mcfg.key} model...")
    t0 = time.time()
    model, device = _load_sentence_transformer(mcfg)
    print(f"  Model loaded on {device} in {time.time() - t0:.1f}s")

    print(f"\n[2/3] Streaming {len(rows)} doc chunks into LanceDB...")
    writer_fn, optimize_cb, get_table = _open_or_create_writer(lance_dir, only_repos, force)
    embedded = _embed_and_write_streaming(
        model,
        rows,
        mcfg,
        writer_fn,
        optimize_cb,
        checkpoint_path=checkpoint_path,
        log_every=log_every,
    )

    print(f"\n[3/3] Finalising LanceDB at {lance_dir} ...")
    table = get_table()
    vectors_stored = 0
    if table is not None:
        with contextlib.suppress(Exception):
            table.optimize()
        try:
            vectors_stored = table.count_rows()
        except Exception:
            vectors_stored = embedded
        if not no_reindex:
            if only_repos and not force:
                _reindex_with_replace(table, vectors_stored, mcfg.dim)
            else:
                _build_ivfpq_index(table, vectors_stored, mcfg.dim)

    # Clear checkpoint when a full build completes — a later resume would be
    # stale because `--force` dropped and re-created the table.
    if force and checkpoint_path and checkpoint_path.exists():
        with contextlib.suppress(Exception):
            checkpoint_path.unlink()
            print("  Checkpoint file removed (force build complete).")

    return {
        "chunks_embedded": embedded,
        "vectors_stored": vectors_stored,
        "lance_path": str(lance_dir),
    }

__all__ = [
    "DOC_FILE_TYPES",
    "build_docs_vectors",
    "fetch_doc_chunks",
]