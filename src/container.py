"""Dependency injection container — DB connections, embedding/reranker providers.

Provides:
- SQLite database connections (lazy, per-call)
- Embedding provider (local SentenceTransformer)
- Reranker provider (local CrossEncoder)
- LanceDB vector table

Models are NOT preloaded at startup. The daemon starts light (~200 MB);
local models are loaded on first use and cached in the provider singletons.
"""

from __future__ import annotations

import contextlib
import functools
import sqlite3
import threading
from collections.abc import Callable, Generator
from typing import Any

from src.config import CACHE_SIZE, DB_PATH, DB_TASKS_PATH, EMBEDDING_MODEL_KEY, MMAP_SIZE

# --- Lazy-loaded singletons ---
_lance_table: Any = None
_wal_set: bool = False
_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    """Get a new database connection. Caller must close it.

    Prefer using ``db_connection()`` context manager for automatic cleanup.

    Attaches ``tasks.db`` so supplementary tables (task_history,
    internal_traces, method_matrix, etc.) are accessible via the same
    connection without qualifying table names. This survives full
    rebuilds of ``knowledge.db`` because tasks.db is a separate file.
    """
    global _wal_set
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not _wal_set:
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_set = True
    conn.execute(f"PRAGMA mmap_size={MMAP_SIZE}")
    conn.execute(f"PRAGMA cache_size={CACHE_SIZE}")
    # Attach supplementary DB so task_history et al. are transparent.
    if DB_TASKS_PATH.exists():
        conn.execute(f"ATTACH DATABASE '{DB_TASKS_PATH}' AS tasks")
    return conn


@contextlib.contextmanager
def db_connection() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for safe database connections.

    Usage::

        with db_connection() as conn:
            rows = conn.execute("SELECT ...").fetchall()
        # conn is automatically closed here
    """
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def check_db_health() -> str | None:
    """Verify knowledge.db exists and has expected tables.

    Returns error message string or None if healthy.
    """
    if not DB_PATH.exists():
        return "Knowledge base not built yet. Run: python3 scripts/build_index.py"
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            missing = {"chunks", "repos", "build_info"} - tables
            if missing:
                return f"Knowledge base incomplete (missing: {', '.join(sorted(missing))}). Run: python3 scripts/build_index.py"
        finally:
            conn.close()
    except sqlite3.Error as e:
        return f"Knowledge base error: {e}. Run: python3 scripts/build_index.py"
    return None


def get_vector_search() -> tuple[Any, Any, str | None]:
    """Get embedding provider and LanceDB table.

    Returns (embedding_provider, lance_table, error_string | None).
    The provider has .embed(texts, task_type) method.
    """
    global _lance_table
    from src.embedding_provider import get_embedding_provider

    provider, warning = get_embedding_provider()

    if _lance_table is not None:
        return provider, _lance_table, warning

    with _lock:
        if _lance_table is not None:
            return provider, _lance_table, warning
        try:
            import lancedb

            from src.models import get_model_config

            mcfg = get_model_config(EMBEDDING_MODEL_KEY)
            lance_path = DB_PATH.parent / mcfg.lance_dir

            if not lance_path.exists():
                return provider, None, f"No vector table at {lance_path}. Run: python3 scripts/build_vectors.py"

            db = lancedb.connect(str(lance_path))
            _lance_table = db.open_table("chunks")
        except Exception as e:
            return provider, None, str(e)

    return provider, _lance_table, warning


def get_reranker() -> tuple[Any, str | None]:
    """Get reranker provider.

    Returns (reranker_provider, error_string | None).
    The provider has .rerank(query, documents) method.
    """
    from src.embedding_provider import get_reranker_provider

    provider, warning = get_reranker_provider()
    return provider, warning


def require_db[**P, T](func: Callable[P, T]) -> Callable[P, T]:
    """Decorator that checks DB health before running a tool function."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        db_err = check_db_health()
        if db_err:
            return db_err  # type: ignore[return-value]
        return func(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def is_model_loaded() -> bool:
    """Check if embedding provider is ready."""
    from src.embedding_provider import _embedding_provider

    return _embedding_provider is not None


def is_reranker_loaded() -> bool:
    """Check if reranker provider is ready."""
    from src.embedding_provider import _reranker_provider

    return _reranker_provider is not None
