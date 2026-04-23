"""Docs-tower vector indexer (two-tower migration, 2026-04-23).

Reads doc-flavoured chunks from ``db/knowledge.db`` and writes embeddings to
``db/vectors.lance.docs/`` using the ``nomic-embed-text-v1.5`` model configured
under ``src/models.EMBEDDING_MODELS["docs"]``.

Why a separate module (not reuse scripts/build_vectors.py):
  - ``build_vectors.py`` is a CLI driver that embeds ALL chunks for the code
    tower. The docs tower embeds only a subset (DOC_FILE_TYPES) and is called
    from the build orchestrator, not from argv.
  - The provider abstraction (LocalEmbeddingProvider) does not expose
    batch_size / show_progress_bar, so we load SentenceTransformer directly
    here (same pattern as build_vectors.py).

Interface:
    build_docs_vectors(db_path, lance_dir, *, force=False,
                       checkpoint_path=None, only_repos=None, log_every=500)
    -> {"chunks_embedded": int, "vectors_stored": int, "lance_path": str}
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import time
from pathlib import Path

# File types that live in the docs tower. Code types (service / workflow /
# frontend / provider_config / test_script / code_file) stay in the coderank
# tower. Keep this list in sync with profiles/*/docs/ layout.
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
_VALID_REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ------------------------------- DB helpers -----------------------------------


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


# ---------------------------- Checkpoint helpers ------------------------------


def _load_checkpoint(path: Path | None) -> tuple[set[int], list[dict]]:
    if path is None or not path.exists():
        return set(), []
    try:
        with open(path) as f:
            ck = json.load(f)
        return set(ck.get("done_rowids", [])), list(ck.get("data", []))
    except Exception as e:
        print(f"  [checkpoint] failed to load ({e}); starting fresh")
        return set(), []


def _save_checkpoint(path: Path | None, done_rowids: list[int], data: list[dict]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump({"done_rowids": done_rowids, "data": data}, f)
    tmp.rename(path)
    print(f"  [checkpoint] saved {len(done_rowids)} embeddings to {path.name}", flush=True)


# -------------------------- Text preparation ----------------------------------


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


def _embed_adaptive(
    model,
    rows: list[tuple],
    mcfg,
    checkpoint_path: Path | None,
    log_every: int,
) -> list[dict]:
    """Adaptive short/long batching with checkpoints. Mirrors build_vectors.py."""
    done_rowids, all_data = _load_checkpoint(checkpoint_path)
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
        return all_data

    start = time.time()
    since_checkpoint = 0
    since_log = 0

    # Short chunks in batches
    for i in range(0, len(short_rows), mcfg.batch_size):
        batch_rows = short_rows[i : i + mcfg.batch_size]
        texts = [_prepare_text(r[1], r[2], r[4], r[5], mcfg.short_limit, mcfg.document_prefix) for r in batch_rows]
        vectors = _encode(model, texts, mcfg.batch_size)
        for row, vec in zip(batch_rows, vectors, strict=True):
            all_data.append(_make_record(row, vec))
            done_rowids.add(row[0])
        done += len(batch_rows)
        since_checkpoint += len(batch_rows)
        since_log += len(batch_rows)

        if since_log >= log_every:
            print("  " + _progress(done, total, start, remaining_at_start), flush=True)
            since_log = 0
        if since_checkpoint >= CHECKPOINT_EVERY:
            _save_checkpoint(checkpoint_path, list(done_rowids), all_data)
            since_checkpoint = 0

    # Long chunks one by one
    if mcfg.long_limit > mcfg.short_limit:
        for row in long_rows:
            text = _prepare_text(row[1], row[2], row[4], row[5], mcfg.long_limit, mcfg.document_prefix)
            vectors = _encode(model, [text], mcfg.batch_size)
            all_data.append(_make_record(row, vectors[0]))
            done_rowids.add(row[0])
            done += 1
            since_checkpoint += 1
            since_log += 1

            if since_log >= log_every:
                print("  " + _progress(done, total, start, remaining_at_start), flush=True)
                since_log = 0
            if since_checkpoint >= CHECKPOINT_EVERY:
                _save_checkpoint(checkpoint_path, list(done_rowids), all_data)
                since_checkpoint = 0

    _save_checkpoint(checkpoint_path, list(done_rowids), all_data)
    print(f"\n  Docs tower: {len(all_data)} embeddings in {time.time() - start:.1f}s")
    return all_data


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


def _store_full(data: list[dict], lance_dir: Path, dim: int, *, no_reindex: bool = False):
    import lancedb

    print(f"\n  Storing {len(data)} vectors in LanceDB at {lance_dir} ...")
    start = time.time()
    db = lancedb.connect(str(lance_dir))
    with contextlib.suppress(Exception):
        db.drop_table("chunks")
    table = db.create_table("chunks", data=data)
    num_vectors = table.count_rows()
    print(f"  Stored {num_vectors} vectors in {time.time() - start:.1f}s")
    if no_reindex:
        print("  Skipping ANN index build (--no-reindex)")
    else:
        _build_ivfpq_index(table, num_vectors, dim)
    return table


def _store_incremental(
    data: list[dict],
    lance_dir: Path,
    only_repos: set[str],
    dim: int,
    *,
    no_reindex: bool = False,
):
    import lancedb

    for r in only_repos:
        if not _VALID_REPO_RE.match(r):
            raise RuntimeError(f"invalid repo name '{r}' — aborting docs vector update")

    db = lancedb.connect(str(lance_dir))
    try:
        table = db.open_table("chunks")
    except Exception:
        # No existing table — fall back to a full write for those repos.
        table = db.create_table("chunks", data=data)
        num_vectors = table.count_rows()
        if no_reindex:
            print("  Skipping ANN index build (--no-reindex)")
        else:
            _build_ivfpq_index(table, num_vectors, dim)
        return table

    repo_filter = " OR ".join(f"repo_name = '{r}'" for r in only_repos)
    table.delete(repo_filter)
    print(f"  Deleted old docs vectors for: {', '.join(sorted(only_repos))}")
    if data:
        table.add(data)
        print(f"  Added {len(data)} new docs vectors")
    with contextlib.suppress(Exception):
        table.optimize()
    if no_reindex:
        print("  Skipping ANN reindex (--no-reindex)")
    else:
        _reindex_with_replace(table, table.count_rows(), dim)
    return table


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
) -> dict:
    """Embed all doc-flavoured chunks from ``db_path`` into ``lance_dir``.

    Returns a summary dict: chunks_embedded, vectors_stored, lance_path.

    ``no_reindex`` skips IVF-PQ index (re)build — useful when chaining multiple
    incremental runs that share one reindex at the end.
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

    # Full vs incremental mode
    is_incremental = bool(only_repos) and lance_dir.exists() and not force
    if is_incremental:
        print(f"\n[2/3] Embedding {len(rows)} docs chunks for changed repos...")
        data = _embed_adaptive(model, rows, mcfg, checkpoint_path=None, log_every=log_every)
        print(f"\n[3/3] Updating LanceDB at {lance_dir} ...")
        lance_dir.parent.mkdir(parents=True, exist_ok=True)
        table = _store_incremental(data, lance_dir, only_repos or set(), mcfg.dim, no_reindex=no_reindex)
    else:
        print(f"\n[2/3] Embedding {len(rows)} docs chunks...")
        data = _embed_adaptive(model, rows, mcfg, checkpoint_path=checkpoint_path, log_every=log_every)
        print(f"\n[3/3] Writing LanceDB at {lance_dir} ...")
        lance_dir.parent.mkdir(parents=True, exist_ok=True)
        table = _store_full(data, lance_dir, mcfg.dim, no_reindex=no_reindex)
        if checkpoint_path and checkpoint_path.exists():
            with contextlib.suppress(Exception):
                checkpoint_path.unlink()
                print("  Checkpoint file removed.")

    vectors_stored = 0
    try:
        vectors_stored = table.count_rows()
    except Exception:
        vectors_stored = len(data)

    return {
        "chunks_embedded": len(data),
        "vectors_stored": vectors_stored,
        "lance_path": str(lance_dir),
    }


__all__ = [
    "DOC_FILE_TYPES",
    "build_docs_vectors",
    "fetch_doc_chunks",
]
