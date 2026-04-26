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

_lance_tables: dict[str, Any] = {}
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
    with _lock:
        if not _wal_set:
            conn.execute("PRAGMA journal_mode=WAL")
            _wal_set = True
    conn.execute(f"PRAGMA mmap_size={MMAP_SIZE}")
    conn.execute(f"PRAGMA cache_size={CACHE_SIZE}")
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


def get_vector_search(model_key: str | None = None) -> tuple[Any, Any, str | None]:
    """Get embedding provider and LanceDB table for the given model key.

    Pass `model_key="docs"` for the docs tower; omit for the configured default
    (EMBEDDING_MODEL_KEY, typically "coderank"). Per-key caching so both towers
    stay open once loaded.

    Returns (embedding_provider, lance_table, error_string | None).
    The provider has .embed(texts, task_type) method.
    """
    global _lance_table
    from src.embedding_provider import get_embedding_provider

    key = model_key or EMBEDDING_MODEL_KEY
    provider, warning = get_embedding_provider(key)

    if key in _lance_tables:
        return provider, _lance_tables[key], warning

    with _lock:
        if key in _lance_tables:
            return provider, _lance_tables[key], warning
        try:
            import lancedb

            from src.models import get_model_config

            mcfg = get_model_config(key)
            lance_path = DB_PATH.parent / mcfg.lance_dir

            if not lance_path.exists():
                return (
                    provider,
                    None,
                    f"No vector table at {lance_path}. Run: python3 scripts/build_vectors.py --model {key}",
                )

            db = lancedb.connect(str(lance_path))
            table = db.open_table("chunks")
            _lance_tables[key] = table
            if _lance_table is None:
                _lance_table = table
        except Exception as e:
            return provider, None, str(e)

    return provider, _lance_tables[key], warning


def get_reranker(intent: str | None = None) -> tuple[Any, str | None]:
    """Get reranker provider.

    intent="code" → l12 FT (Tarshevskiy/pay-com-rerank-l12-ft-run1, +3.31pp top-10 vs L6 on jira n=908).
    intent="docs" or None → default L6 baseline.

    Returns (reranker_provider, error_string | None).
    The provider has .rerank(query, documents) method.
    """
    from src.embedding_provider import get_reranker_provider

    provider, warning = get_reranker_provider(intent=intent)
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
    """Check if any embedding provider is ready."""
    from src.embedding_provider import _embedding_providers

    return bool(_embedding_providers)


def is_reranker_loaded() -> bool:
    """Check if reranker provider is ready."""
    from src.embedding_provider import _reranker_provider

    return _reranker_provider is not None
