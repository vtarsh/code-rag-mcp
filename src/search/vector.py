"""LanceDB vector similarity search.

Handles embedding generation and vector search with optional filters.
Uses embedding provider (API or local) from container.
"""

from __future__ import annotations

import logging

from src.container import get_vector_search

log = logging.getLogger(__name__)


def vector_search(
    query: str,
    repo: str = "",
    file_type: str = "",
    exclude_file_types: str = "",
    limit: int = 20,
) -> tuple[list[dict], str | None]:
    """Run vector similarity search.

    Returns (results_list, error_string | None).
    Results are raw dicts from LanceDB (not yet converted to SearchResult).
    """
    provider, table, err = get_vector_search()
    if err and table is None:
        return [], err
    if provider is None or table is None:
        return [], "Vector search unavailable: provider or table not loaded"

    # Embed query using provider (handles prefixing and API calls internally)
    try:
        vectors = provider.embed([query], task_type="query")
        embedding = vectors[0]
    except Exception as e:
        return [], f"Embedding failed: {e}"

    # Build filter — sanitize inputs to prevent injection in LanceDB WHERE clause
    filters: list[str] = []
    if repo:
        safe_repo = repo.replace("'", "''").replace("%", "").replace("_", "\\_")
        filters.append(f"repo_name LIKE '%{safe_repo}%'")
    if file_type:
        safe_ft = file_type.replace("'", "''")
        filters.append(f"file_type = '{safe_ft}'")
    if exclude_file_types:
        for ft in exclude_file_types.split(","):
            ft = ft.strip()
            if ft:
                safe_ft = ft.replace("'", "''")
                filters.append(f"file_type != '{safe_ft}'")

    where = " AND ".join(filters) if filters else None

    try:
        results = table.search(embedding).where(where).limit(limit).to_list()
        return results, err  # pass through provider warning if any
    except Exception as e:
        # If filter fails, try without — log the filter error
        log.warning(f"Vector filter failed ({where}): {e}, retrying without filter")
        try:
            results = table.search(embedding).limit(limit).to_list()
            return results, f"Filter failed, showing unfiltered results: {e}"
        except Exception as e2:
            return [], str(e2)
