"""Search MCP tools — search().

Public MCP tool function registered with FastMCP.
"""

from __future__ import annotations

from src.cache import cache_key, cache_or_compute
from src.container import require_db
from src.feedback import log_search
from src.formatting import strip_repo_tag
from src.search.fts import expand_query
from src.search.hybrid import hybrid_search
from src.search.suggestions import format_no_results


@require_db
def search_tool(
    query: str = "",
    repo: str = "",
    file_type: str = "",
    exclude_file_types: str = "",
    limit: int = 10,
) -> str:
    """Search the knowledge base using keyword + semantic hybrid search.

    Args:
        query: Search query — keywords or natural language question
        repo: Optional - filter by repo name (exact or partial match)
        file_type: Optional - filter by type: proto, docs, config, env, k8s, grpc_method, library, workflow, ci, gotchas, reference, dictionary, flow_annotation, task, provider_doc, domain_registry
        exclude_file_types: Optional - comma-separated file types to exclude from results (e.g. "gotchas,task")
        limit: Max results to return (default 10, max 20)
    """
    # Defensive validation: callers sometimes omit `query` entirely (observed 74x
    # KeyError('query') in logs/tool_calls.jsonl before this guard was added).
    # Return a clear error rather than a Python traceback.
    if query is None or not isinstance(query, str) or not query.strip():
        return (
            "Error: 'query' parameter is required and must be a non-empty string. "
            "Example: search(query=\"payment provider integration\")"
        )

    limit = min(max(1, limit), 20)

    expanded = expand_query(query)
    ck = cache_key(
        "search", query=expanded, repo=repo, file_type=file_type, exclude_file_types=exclude_file_types, limit=limit
    )

    def _compute() -> str:
        ranked, vec_err, total_candidates = hybrid_search(expanded, repo, file_type, exclude_file_types, limit)

        log_search("search", expanded, {"repo": repo, "file_type": file_type, "limit": limit}, ranked, total_candidates)

        if not ranked:
            context = ""
            if repo:
                context += f"Filter: repo='{repo}'. "
            if file_type:
                context += f"Filter: type='{file_type}'. "
            return format_no_results(query, context.strip())

        results: list[str] = []
        for r in ranked:
            snippet = strip_repo_tag(r["snippet"])
            sources = "+".join(r["sources"])
            results.append(
                f"**{r['repo_name']}** | `{r['file_path']}` ({r['file_type']}/{r['chunk_type']}) [{sources}]\n"
                f"  {snippet[:300]}\n"
            )

        header = f"Found {len(ranked)} of {total_candidates} candidates for '{query}'"
        if repo:
            header += f" in repos matching '{repo}'"
        if file_type:
            header += f" (type: {file_type})"
        if vec_err:
            header += " (keyword only)"
            header += f"\n⚠️ Vector search unavailable: {vec_err}"

        return header + "\n\n" + "\n".join(results)

    return cache_or_compute(ck, _compute)
