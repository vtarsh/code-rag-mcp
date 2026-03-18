"""LanceDB vector similarity search.

Handles embedding generation and vector search with optional filters.
"""

from __future__ import annotations

from src.config import EMBEDDING_MODEL_KEY
from src.container import get_vector_search
from src.models import get_model_config


def vector_search(
    query: str,
    repo: str = "",
    file_type: str = "",
    limit: int = 20,
) -> tuple[list[dict], str | None]:
    """Run vector similarity search.

    Returns (results_list, error_string | None).
    Results are raw dicts from LanceDB (not yet converted to SearchResult).
    """
    model, table, err = get_vector_search()
    if err:
        return [], f"Vector search unavailable: {err}"
    if model is None or table is None:
        return [], "Vector search unavailable: model or table not loaded"

    # Apply model-specific query prefix (e.g. CodeRankEmbed needs one)
    _mcfg = get_model_config(EMBEDDING_MODEL_KEY)
    prefixed_query = f"{_mcfg.query_prefix}{query}" if _mcfg.query_prefix else query
    embedding = model.encode([prefixed_query])[0].tolist()

    # Build filter — sanitize inputs to prevent injection in LanceDB WHERE clause
    filters: list[str] = []
    if repo:
        safe_repo = repo.replace("'", "''").replace("%", "").replace("_", "\\_")
        filters.append(f"repo_name LIKE '%{safe_repo}%'")
    if file_type:
        safe_ft = file_type.replace("'", "''")
        filters.append(f"file_type = '{safe_ft}'")

    where = " AND ".join(filters) if filters else None

    try:
        results = table.search(embedding).where(where).limit(limit).to_list()
        return results, None
    except Exception:
        # If filter fails, try without
        try:
            results = table.search(embedding).limit(limit).to_list()
            return results, None
        except Exception as e2:
            return [], str(e2)
