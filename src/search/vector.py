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
    model_key: str | None = None,
) -> tuple[list[dict], str | None]:
    """Run vector similarity search.

    Returns (results_list, error_string | None).
    Results are raw dicts from LanceDB (not yet converted to SearchResult).

    `model_key` selects the embedding tower. `None` (default) resolves to the
    configured `EMBEDDING_MODEL_KEY` (code tower, typically "coderank") and
    preserves all pre-two-tower behaviour. Pass `"docs"` to query the nomic
    docs tower (table `vectors.lance.docs`) for doc-intent queries — the
    provider applies the `search_query:` prefix automatically.
    """
    provider, table, err = get_vector_search(model_key)
    if table is None:
        if err:
            return [], err
        return [], "Vector search unavailable: provider or table not loaded"
    if provider is None:
        return [], err or "Vector search unavailable: provider or table not loaded"

    try:
        vectors = provider.embed([query], task_type="query")
        embedding = vectors[0]
    except Exception as e:
        return [], f"Embedding failed: {e}"

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
        # P0e (2026-05-17): request larger ANN pool and filter in Python to avoid
        # LanceDB post-filter returning <limit rows when restrictive WHERE is applied.
        ann_limit = limit * 10 if where else limit
        raw_results = table.search(embedding).where(where).limit(ann_limit).to_list()

        # Post-filter in Python: if LanceDB returned too many due to loose filtering,
        # apply exact filters. If too few, we already got what ANN could find.
        if where and len(raw_results) > limit:
            # Re-apply exact filters manually for precision
            filtered = []
            for r in raw_results:
                if repo and repo not in (r.get("repo_name") or ""):
                    continue
                if file_type and r.get("file_type") != file_type:
                    continue
                if exclude_file_types:
                    excluded = {ft.strip() for ft in exclude_file_types.split(",") if ft.strip()}
                    if r.get("file_type") in excluded:
                        continue
                filtered.append(r)
                if len(filtered) >= limit:
                    break
            results = filtered
        else:
            results = raw_results[:limit]

        distances = [r.get("_distance") for r in results if r.get("_distance") is not None]
        if distances:
            log.debug(
                f"Vector search: model={model_key}, query_len={len(query)}, "
                f"results={len(results)}, distance_range={min(distances):.3f}-{max(distances):.3f}"
            )
        else:
            log.debug(
                f"Vector search: model={model_key}, query_len={len(query)}, results={len(results)}, distance_range=N/A"
            )
        return results, err  # pass through provider warning if any
    except Exception as e:
        log.warning(f"Vector filter failed ({where}): {e}, retrying without filter")
        try:
            results = table.search(embedding).limit(limit).to_list()
            distances = [r.get("_distance") for r in results if r.get("_distance") is not None]
            if distances:
                log.debug(
                    f"Vector search: model={model_key}, query_len={len(query)}, "
                    f"results={len(results)}, distance_range={min(distances):.3f}-{max(distances):.3f}"
                )
            else:
                log.debug(
                    f"Vector search: model={model_key}, query_len={len(query)}, "
                    f"results={len(results)}, distance_range=N/A"
                )
            return results, f"Filter failed, showing unfiltered results: {e}"
        except Exception as e2:
            return [], str(e2)
