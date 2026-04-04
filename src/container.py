"""Dependency injection container — DB connections, embedding/reranker providers.

Provides:
- SQLite database connections (lazy, per-call)
- Embedding provider (API-first with local fallback)
- Reranker provider (API-first with local fallback)
- LanceDB vector table

Models are NOT preloaded at startup. The daemon starts light (~200 MB).
Local models are only loaded as fallback when API is unavailable.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import sqlite3
import threading
from collections.abc import Callable, Generator
from typing import Any, ParamSpec, TypeVar

from src.config import CACHE_SIZE, DB_PATH, EMBEDDING_MODEL_KEY, LANCE_PATH, MMAP_SIZE

# --- Lazy-loaded singletons ---
_lance_table: Any = None
_wal_set: bool = False
_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    """Get a new database connection. Caller must close it.

    Prefer using ``db_connection()`` context manager for automatic cleanup.
    """
    global _wal_set
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not _wal_set:
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_set = True
    conn.execute(f"PRAGMA mmap_size={MMAP_SIZE}")
    conn.execute(f"PRAGMA cache_size={CACHE_SIZE}")
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

            # Determine correct LanceDB path based on active provider
            from src.models import get_model_config

            if "gemini" in provider.provider_name:
                mcfg = get_model_config("gemini")
            else:
                mcfg = get_model_config(EMBEDDING_MODEL_KEY)

            lance_path = DB_PATH.parent / mcfg.lance_dir
            if not lance_path.exists():
                # Gemini vectors not built yet — try local fallback
                local_mcfg = get_model_config(EMBEDDING_MODEL_KEY)
                fallback_path = DB_PATH.parent / local_mcfg.lance_dir
                if fallback_path.exists() and "gemini" in provider.provider_name:
                    logging.warning(
                        f"Gemini vectors not built yet ({lance_path}). "
                        f"Run: python3 scripts/build_vectors.py --model=gemini"
                    )
                    return provider, None, f"Gemini vectors not built. Run: python3 scripts/build_vectors.py --model=gemini"
                lance_path = fallback_path

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


P = ParamSpec("P")
T = TypeVar("T")


def require_db(func: Callable[P, T]) -> Callable[P, T]:
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
