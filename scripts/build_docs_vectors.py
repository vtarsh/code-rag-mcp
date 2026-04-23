#!/usr/bin/env python3
"""Build vector embeddings for the docs tower (two-tower migration, 2026-04-23).

Thin CLI wrapper around `src.index.builders.docs_vector_indexer.build_docs_vectors`.
The heavy lifting (chunk selection, adaptive batching, checkpointing, IVF-PQ index
build) lives in that module so tests + daemon can call it directly.

Model: nomic-ai/nomic-embed-text-v1.5 (768-dim, ~550 MB RAM).
Output: $CODE_RAG_HOME/db/vectors.lance.docs/

Usage:
    python3 build_docs_vectors.py                 # full build, resume from checkpoint
    python3 build_docs_vectors.py --force         # drop LanceDB table, rebuild
    python3 build_docs_vectors.py --repos=a,b,c   # incremental (only these repos)
    python3 build_docs_vectors.py --no-reindex    # skip IVF-PQ index rebuild
    python3 build_docs_vectors.py --help          # this message

See profiles/pay-com/docs/gotchas/two-tower-migration.md for operator runbook.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Resolve paths from environment or defaults ---
BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = BASE_DIR / "db" / "knowledge.db"
LANCE_DIR = BASE_DIR / "db" / "vectors.lance.docs"
CHECKPOINT_PATH = BASE_DIR / "db" / "docs_checkpoint.json"

# --- Add project root to sys.path so `src...` imports resolve from this script ---
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


HELP_TEXT = __doc__ or ""


def _print_help() -> None:
    print(HELP_TEXT.strip())


def parse_args() -> tuple[bool, set[str] | None, bool]:
    """Parse CLI arguments. Returns (force, only_repos, no_reindex)."""
    force = False
    only_repos: set[str] | None = None
    no_reindex = False

    for arg in sys.argv[1:]:
        if arg in {"-h", "--help"}:
            _print_help()
            sys.exit(0)
        elif arg == "--force":
            force = True
        elif arg.startswith("--repos="):
            raw = arg.split("=", 1)[1].strip()
            if raw:
                only_repos = {r.strip() for r in raw.split(",") if r.strip()}
        elif arg == "--no-reindex":
            no_reindex = True
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            print("Run with --help for usage.", file=sys.stderr)
            sys.exit(2)

    return force, only_repos, no_reindex


def main() -> int:
    force, only_repos, no_reindex = parse_args()

    # Basic sanity check — fail fast with an actionable message if the DB is missing.
    if not DB_PATH.exists():
        print(f"ERROR: SQLite DB not found at {DB_PATH}", file=sys.stderr)
        print("Run the indexer first (make build or scripts/build_index.py).", file=sys.stderr)
        return 1

    print("=" * 60)
    print("Building Docs Vector Embeddings (two-tower)")
    print("Model: nomic-ai/nomic-embed-text-v1.5 (768d)")
    print(f"Output: {LANCE_DIR}")
    if only_repos:
        print(f"Mode: incremental ({len(only_repos)} repos)")
    elif force:
        print("Mode: full rebuild (--force)")
    else:
        print("Mode: full build (resume from checkpoint if present)")
    if no_reindex:
        print("IVF-PQ reindex: SKIPPED (--no-reindex)")
    print("=" * 60)

    # Import here so that --help works even if the indexer module isn't wired yet
    # (Agent A may still be landing src/index/builders/docs_vector_indexer.py).
    try:
        from src.index.builders.docs_vector_indexer import build_docs_vectors
    except ImportError as exc:
        print(
            "ERROR: src.index.builders.docs_vector_indexer is not importable.",
            file=sys.stderr,
        )
        print(f"       ({exc})", file=sys.stderr)
        print(
            "       This module is Agent A's responsibility. "
            "If Agent A hasn't landed yet, wait for it or re-run later.",
            file=sys.stderr,
        )
        return 1

    try:
        result = build_docs_vectors(
            db_path=DB_PATH,
            lance_dir=LANCE_DIR,
            force=force,
            checkpoint_path=CHECKPOINT_PATH,
            only_repos=only_repos,
            log_every=500,
            no_reindex=no_reindex,
        )
    except Exception as exc:
        print(f"ERROR: docs vector build failed: {exc}", file=sys.stderr)
        return 1

    # Summary (best-effort — shape of `result` is not strictly specified).
    total_vectors = None
    size_mb = None
    if isinstance(result, dict):
        total_vectors = result.get("total_vectors") or result.get("vectors") or result.get("count")
        size_mb = result.get("size_mb") or result.get("size_on_disk_mb")

    if total_vectors is None and LANCE_DIR.exists():
        try:
            import lancedb  # local import — lancedb is heavy

            db = lancedb.connect(str(LANCE_DIR))
            if "chunks" in db.table_names():
                total_vectors = db.open_table("chunks").count_rows()
        except Exception:
            total_vectors = None

    if size_mb is None and LANCE_DIR.exists():
        try:
            size_mb = sum(f.stat().st_size for f in LANCE_DIR.rglob("*") if f.is_file()) / (1024 * 1024)
        except Exception:
            size_mb = None

    print()
    print("=" * 60)
    print(f"Docs vector store: {LANCE_DIR}")
    print(f"Total vectors: {total_vectors if total_vectors is not None else 'unknown'}")
    print("Dimensions: 768")
    print("Model: nomic-ai/nomic-embed-text-v1.5 (docs)")
    if size_mb is not None:
        print(f"Size on disk: {size_mb:.1f} MB")
    else:
        print("Size on disk: unknown")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
