"""Dependency injection container — DB connections, ML models, preload.

Provides lazy-loaded singletons for:
- SQLite database connections
- SentenceTransformer embedding model
- CrossEncoder reranker model
- LanceDB vector table

Thread-safe model preloading at startup.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import threading
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from src.config import DB_PATH, EMBEDDING_MODEL_KEY, LANCE_PATH
from src.models import get_model_config

# --- Lazy-loaded singletons ---
_model: Any = None
_lance_table: Any = None
_reranker: Any = None
_wal_set: bool = False
_lock = threading.Lock()


def get_db() -> sqlite3.Connection:
    """Get a new database connection. Caller must close it."""
    global _wal_set
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not _wal_set:
        conn.execute("PRAGMA journal_mode=WAL")
        _wal_set = True
    conn.execute("PRAGMA mmap_size=268435456")  # 256MB mmap for faster reads
    conn.execute("PRAGMA cache_size=-32000")  # 32MB page cache
    return conn


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
    """Lazy-load embedding model and LanceDB table.

    Returns (model, table, error_string | None).
    """
    global _model, _lance_table
    if _model is not None:
        return _model, _lance_table, None
    with _lock:
        if _model is not None:  # double-check after acquiring lock
            return _model, _lance_table, None
        try:
            import lancedb
            from sentence_transformers import SentenceTransformer

            _mcfg = get_model_config(EMBEDDING_MODEL_KEY)
            _model = SentenceTransformer(_mcfg.name, trust_remote_code=_mcfg.trust_remote_code)
            db = lancedb.connect(str(LANCE_PATH))
            _lance_table = db.open_table("chunks")
        except Exception as e:
            return None, None, str(e)
    return _model, _lance_table, None


def get_reranker() -> tuple[Any, str | None]:
    """Lazy-load cross-encoder reranker model.

    Returns (reranker, error_string | None).
    """
    global _reranker
    if _reranker is not None:
        return _reranker, None
    with _lock:
        if _reranker is not None:  # double-check after acquiring lock
            return _reranker, None
        try:
            from sentence_transformers import CrossEncoder

            _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        except Exception as e:
            return None, str(e)
    return _reranker, None


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
    """Check if embedding model is loaded."""
    return _model is not None


def is_reranker_loaded() -> bool:
    """Check if reranker model is loaded."""
    return _reranker is not None


def preload_models() -> None:
    """Preload embedding model and reranker. Called from background thread."""
    try:
        get_vector_search()
        get_reranker()
        logging.info("Models preloaded successfully")
    except Exception as e:
        logging.warning(f"Model preload failed (will retry on first query): {e}")


def start_preload() -> None:
    """Start model preloading in a daemon thread."""
    threading.Thread(target=preload_models, daemon=True).start()
