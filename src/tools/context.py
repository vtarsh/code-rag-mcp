"""context_builder MCP tool — gather comprehensive context in a single call.

Combines hybrid search + dependency graph + proto definitions into one
optimized context block for LLM tasks. Replaces the typical 3-4 sequential
tool calls (search → find_deps → repo_overview) with one call.
"""

from __future__ import annotations

from pathlib import Path

from src.cache import cache_get, cache_key, cache_set
from src.container import get_db, require_db
from src.formatting import strip_repo_tag
from src.graph.queries import get_incoming_edges, get_outgoing_edges
from src.search.fts import expand_query
from src.search.hybrid import hybrid_search


@require_db
def context_builder_tool(
    query: str,
    repo: str = "",
    include_deps: bool = True,
    include_proto: bool = True,
    search_limit: int = 8,
) -> str:
    """Build comprehensive context for a development task in one call.

    Combines:
    1. Hybrid search results (keyword + vector + reranker)
    2. Dependency graph for each discovered repo (who calls it, what it calls)
    3. Proto definitions relevant to the query

    Args:
        query: What you're working on (e.g., "add refund support to Trustly", "settlement reconciliation flow")
        repo: Optional — focus on a specific repo
        include_deps: Include dependency graph for discovered repos (default: true)
        include_proto: Include proto definitions if found (default: true)
        search_limit: Max search results (default 8)
    """
    if not query.strip():
        return "Error: query cannot be empty"

    ck = cache_key(
        "context_builder",
        query=query,
        repo=repo,
        include_deps=include_deps,
        include_proto=include_proto,
        search_limit=search_limit,
    )
    cached = cache_get(ck)
    if cached is not None:
        return cached

    result = _build_context(query, repo, include_deps, include_proto, search_limit)
    cache_set(ck, result)
    return result


def _build_context(
    query: str,
    repo: str,
    include_deps: bool,
    include_proto: bool,
    search_limit: int,
) -> str:
    expanded = expand_query(query)
    output = f"# Context: {query}\n\n"

    # 1. Search
    ranked, vec_err, _total = hybrid_search(expanded, repo, "", search_limit)
    if not ranked:
        return output + "No results found. Try different keywords.\n"

    # Collect unique repos from results
    seen_repos: dict[str, list[dict]] = {}
    for r in ranked:
        seen_repos.setdefault(r["repo_name"], []).append(r)

    output += f"## Search Results ({len(ranked)} matches across {len(seen_repos)} repos)\n\n"
    for r in ranked:
        snippet = strip_repo_tag(r["snippet"])
        sources = "+".join(r["sources"])
        output += (
            f"**{r['repo_name']}** | `{r['file_path']}` "
            f"({r['file_type']}/{r['chunk_type']}) [{sources}]\n"
            f"  {snippet[:250]}\n\n"
        )

    if vec_err:
        output += f"⚠️ Vector search unavailable: {vec_err}\n\n"

    # 2. Dependencies
    if include_deps and seen_repos:
        output += _build_deps_section(list(seen_repos.keys()))

    # 3. Proto
    if include_proto:
        output += _build_proto_section(expanded, query, list(seen_repos.keys()))

    # 4. Repo summary
    output += _build_repo_summary(list(seen_repos.keys()))

    return output


def _build_deps_section(repo_names: list[str]) -> str:
    """Build compact dependency overview for discovered repos."""
    conn = get_db()
    try:
        output = f"## Dependencies ({len(repo_names)} repos)\n\n"

        for repo_name in repo_names[:6]:  # cap to avoid huge output
            outgoing = get_outgoing_edges(conn, repo_name)
            incoming = get_incoming_edges(conn, repo_name)

            if not outgoing and not incoming:
                continue

            output += f"### {repo_name}\n"

            # Compact outgoing: group by type, show max 5 per type
            if outgoing:
                by_type: dict[str, list[str]] = {}
                for e in outgoing:
                    if not e.target.startswith(("pkg:", "proto:", "msg:", "svc:")):
                        by_type.setdefault(e.edge_type, []).append(e.target)
                if by_type:
                    output += "**Depends on**: "
                    parts: list[str] = []
                    for etype, targets in sorted(by_type.items()):
                        unique = sorted(set(targets))
                        shown = unique[:5]
                        suffix = f" +{len(unique) - 5}" if len(unique) > 5 else ""
                        parts.append(f"{etype}: {', '.join(shown)}{suffix}")
                    output += " | ".join(parts) + "\n"

            # Compact incoming: just count by type
            if incoming:
                by_type_in: dict[str, int] = {}
                for e in incoming:
                    if not e.source.startswith(("pkg:", "proto:", "msg:", "svc:")):
                        by_type_in[e.edge_type] = by_type_in.get(e.edge_type, 0) + 1
                if by_type_in:
                    output += "**Used by**: "
                    parts = [f"{cnt} via {etype}" for etype, cnt in sorted(by_type_in.items(), key=lambda x: -x[1])]
                    output += ", ".join(parts) + "\n"

            output += "\n"

        return output
    finally:
        conn.close()


def _build_proto_section(
    expanded_query: str,
    raw_query: str,
    relevant_repos: list[str] | None = None,
) -> str:
    """Find relevant proto definitions, prioritizing results from repos in search hits."""
    conn = get_db()
    try:
        fts_q = _sanitize_for_fts(expanded_query)
        if not fts_q:
            return ""

        proto_chunks = conn.execute(
            """SELECT repo_name, file_path, chunk_type,
                      snippet(chunks, 0, '>>>', '<<<', '...', 30) as snippet,
                      rank
               FROM chunks
               WHERE chunks MATCH ? AND file_type = 'proto'
               ORDER BY rank LIMIT 20""",
            (fts_q,),
        ).fetchall()

        if not proto_chunks:
            return ""

        # Separate into relevant (from search-hit repos or proto/libs repos) vs noise
        priority_prefixes = {"providers-proto", "libs-types", "grpc-core-schemas"}
        if relevant_repos:
            priority_prefixes.update(relevant_repos)

        # Sort: priority repos first, then others (including envoy — never exclude)
        priority = []
        other = []
        for row in proto_chunks:
            if row["repo_name"] in priority_prefixes:
                priority.append(row)
            else:
                other.append(row)

        # Show priority first, then fill up to 8 total
        selected = priority[:8]
        remaining = 8 - len(selected)
        if remaining > 0:
            selected.extend(other[:remaining])

        if not selected:
            return ""

        output = "## Proto Definitions\n\n"
        for row in selected:
            snippet = strip_repo_tag(row["snippet"])
            output += f"**{row['repo_name']}** | `{row['file_path']}` ({row['chunk_type']})\n  {snippet[:300]}\n\n"
        return output
    except Exception:
        return ""
    finally:
        conn.close()


_STOP_WORDS = frozenset(
    {
        "add",
        "get",
        "set",
        "use",
        "new",
        "the",
        "for",
        "and",
        "with",
        "from",
        "how",
        "does",
        "what",
        "this",
        "that",
        "into",
        "make",
        "call",
        "need",
        "want",
        "help",
        "show",
        "find",
        "look",
        "check",
        "support",
        "change",
        "update",
        "create",
        "delete",
        "remove",
        "implement",
        "about",
        "where",
    }
)


def _sanitize_for_fts(query: str) -> str:
    """Sanitize query for proto FTS search — strip stop words, OR mode.

    Uses OR because proto files rarely contain provider-specific terms,
    so AND would be too restrictive. Stop-word removal eliminates the
    noise (e.g. 'add', 'support' matching envoy-proto).
    """
    tokens = query.split()
    sanitized = [t for t in tokens if len(t) >= 3 and t.lower() not in _STOP_WORDS]
    if not sanitized:
        sanitized = [t for t in tokens if len(t) >= 3]
    return " OR ".join(sanitized) if sanitized else ""


def _build_repo_summary(repo_names: list[str]) -> str:
    """Build a compact summary of discovered repos."""
    conn = get_db()
    try:
        output = "## Repo Summary\n\n"
        output += "| Repo | Type | Methods |\n|------|------|--------|\n"

        for repo_name in repo_names[:10]:
            repo = conn.execute("SELECT type FROM repos WHERE name = ?", (repo_name,)).fetchone()
            if not repo:
                continue
            methods = conn.execute(
                "SELECT DISTINCT file_path FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method'", (repo_name,)
            ).fetchall()
            method_names = [Path(m["file_path"]).stem for m in methods]
            methods_str = ", ".join(method_names[:5])
            if len(method_names) > 5:
                methods_str += f" +{len(method_names) - 5}"
            output += f"| {repo_name} | {repo['type']} | {methods_str or '-'} |\n"

        output += "\n"
        return output
    finally:
        conn.close()
