"""MCP Server — tool registration and entry point.

All tools are registered here with FastMCP.
Business logic lives in service modules, this file only wires them up.

Tools (13):
  Search:  search, search_task_history
  Graph:   find_dependencies, trace_impact, trace_flow, trace_chain
  Tools:   repo_overview, list_repos, analyze_task, context_builder, health_check, visualize_graph
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.cache import tracked
from src.config import SERVER_NAME
from src.container import start_preload
from src.graph.service import (
    find_dependencies_tool,
    trace_chain_tool,
    trace_flow_tool,
    trace_impact_tool,
)
from src.search.service import search_tool
from src.tools.analyze import analyze_task_tool
from src.tools.context import context_builder_tool
from src.tools.fields import trace_field_tool
from src.tools.service import (
    diff_provider_config_tool,
    health_check_tool,
    list_repos_tool,
    repo_overview_tool,
    search_task_history_tool,
    visualize_graph_tool,
)

mcp = FastMCP(SERVER_NAME)


# --- Search tools ---


@mcp.tool()
@tracked
def search(query: str, repo: str = "", file_type: str = "", exclude_file_types: str = "", limit: int = 10) -> str:
    """Search the knowledge base using keyword + semantic hybrid search.

    Works for both keyword queries ("user authentication", "webhook callback")
    and natural language questions ("which services handle user sessions",
    "how does authentication work"). Uses hybrid search (FTS5 + vector + reranker)
    internally to find the best results regardless of query style.

    Args:
        query: Search query — keywords or natural language question
        repo: Optional - filter by repo name (exact or partial match)
        file_type: Optional - filter by type: proto, docs, config, env, k8s, grpc_method, library, workflow, ci, gotchas
        exclude_file_types: Optional - comma-separated file types to exclude from results (e.g. "gotchas,task")
        limit: Max results to return (default 10, max 20)
    """
    return search_tool(query, repo, file_type, exclude_file_types, limit)


@mcp.tool()
@tracked
def search_task_history(query: str, developer: str = "", limit: int = 10) -> str:
    """Search past tasks by description, repos, files, or any keyword.

    Args:
        query: Search query — keywords or natural language question
        developer: Optional - filter by developer name (partial match)
        limit: Max results to return (default 10, max 20)
    """
    return search_task_history_tool(query, developer, limit)


# --- Graph tools ---


@mcp.tool()
@tracked
def find_dependencies(repo_name: str) -> str:
    """Find what a repo depends on AND what depends on it.

    Args:
        repo_name: Exact repo name
    """
    return find_dependencies_tool(repo_name)


@mcp.tool()
@tracked
def trace_impact(repo_name: str, depth: int = 2) -> str:
    """Trace transitive impact: which repos are affected if this repo changes.

    Uses the dependency graph to find all repos that directly or transitively
    depend on the given repo. Essential for "what might I miss" analysis before PRs.

    Args:
        repo_name: Repo to trace impact from (e.g., "shared-proto", "common-types")
        depth: How many levels deep to trace (default 2, max 4)
    """
    return trace_impact_tool(repo_name, depth)


@mcp.tool()
@tracked
def trace_flow(source: str, target: str, max_depth: int = 5) -> str:
    """Find the shortest path(s) between two repos/services in the dependency graph.

    Answers questions like "how does api-gateway connect to user-service?"

    Args:
        source: Starting repo (e.g., "api-gateway", "web-app")
        target: Destination repo (e.g., "user-service", "notification-worker")
        max_depth: Maximum hops to search (default 5, max 8)
    """
    return trace_flow_tool(source, target, max_depth)


@mcp.tool()
@tracked
def trace_chain(start: str, direction: str = "both", max_depth: int = 4) -> str:
    """Trace the processing chain through services starting from a repo or concept.

    Unlike trace_flow (which finds A→B paths), this explores the full call chain
    from a starting point — upstream (who calls it) and downstream (what it calls).

    Args:
        start: A repo name OR a concept name (as defined in your profile's known_flows.yaml)
        direction: "downstream" (what it calls), "upstream" (who calls it), or "both" (default)
        max_depth: How many hops to follow (default 4, max 6)
    """
    return trace_chain_tool(start, direction, max_depth)


@mcp.tool()
@tracked
def trace_field(field: str, provider: str = "", mode: str = "trace") -> str:
    """Trace a field through the service chain — from producer to final consumer.

    Core principle: every field change must be traced through ALL services.

    Args:
        field: Field name to trace (e.g., "processorTransactionId", "finalize.issuerResponseCode")
        provider: Optional provider name to filter results (e.g., "payper", "volt")
        mode: Query type — "trace" (full chain), "consumers" (who reads it),
              "compare" (cross-provider), "contract" (field spec)
    """
    return trace_field_tool(field, provider, mode)


# --- Utility tools ---


@mcp.tool()
@tracked
def repo_overview(repo_name: str) -> str:
    """Get detailed overview of a specific repo.

    Args:
        repo_name: Exact repo name (e.g., "my-api-service", "notification-worker")
    """
    return repo_overview_tool(repo_name)


@mcp.tool()
@tracked
def list_repos(type: str = "", has_dep: str = "", limit: int = 30) -> str:
    """List repos filtered by type or dependency.

    Args:
        type: Filter by repo type: grpc-service-js, grpc-service-ts, temporal-workflow, library, boilerplate, node-service, ci-actions, gitops
        has_dep: Filter repos that depend on this package (e.g., "shared-proto", "common-types")
        limit: Max results (default 30)
    """
    return list_repos_tool(type, has_dep, limit)


@mcp.tool()
@tracked
def diff_provider_config(provider_a: str, provider_b: str) -> str:
    """Compare feature flags and config between two providers from seeds.cql.

    Useful for understanding why a feature works for one provider but not another.
    Shows differences in payment_method_type, feature flags, and supported operations.

    Args:
        provider_a: First provider name (e.g., "trustly", "epx")
        provider_b: Second provider name (e.g., "paypal", "nuvei")
    """
    return diff_provider_config_tool(provider_a, provider_b)


@mcp.tool()
@tracked
def analyze_task(description: str, provider: str = "", rerank: bool = False) -> str:
    """Analyze a development task and find ALL relevant repos, files, and dependencies.

    Takes a task description (e.g., "add caching to user-service") and automatically:
    1. Identifies relevant repos, activities, and service methods
    2. Checks proto contracts for required methods
    3. Traces the dependency graph for affected repos
    4. Searches GitHub for existing PRs/branches related to this task
    5. Generates a completeness report and change checklist

    Args:
        description: Task description (e.g., "implement rate limiting for api-gateway")
        provider: Optional provider/service name to focus on
        rerank: Set rerank=true to filter predictions via Gemini 3.1 Pro (requires GEMINI_API_KEY)
    """
    return analyze_task_tool(description, provider, rerank)


@mcp.tool()
@tracked
def context_builder(
    query: str,
    repo: str = "",
    include_deps: bool = True,
    include_proto: bool = True,
    search_limit: int = 8,
) -> str:
    """Build comprehensive context for a development task in one call.

    Combines search results + dependency graph + proto definitions into one
    optimized block. Use this instead of calling search → find_dependencies →
    repo_overview separately.

    Best for: "I need to understand X before making changes" or
    "gather all context about Y for implementation planning".

    Args:
        query: What you're working on (e.g., "add caching to user-service", "authentication flow")
        repo: Optional — focus on a specific repo
        include_deps: Include dependency graph for discovered repos (default: true)
        include_proto: Include proto definitions if found (default: true)
        search_limit: Max search results (default 8)
    """
    return context_builder_tool(query, repo, include_deps, include_proto, search_limit)


@mcp.tool()
@tracked
def health_check() -> str:
    """Return a diagnostic report on the knowledge base: database, vector store,
    models, graph, and consistency status. Takes no arguments."""
    return health_check_tool()


@mcp.tool()
@tracked
def visualize_graph(repo: str = "", edge_type: str = "") -> str:
    """Generate an interactive D3.js graph visualization and open it in the browser.

    Args:
        repo: Optional — focus on a specific repo's neighborhood (e.g., "my-api-service")
        edge_type: Optional — show only a specific edge type (e.g., "grpc_call", "grpc_client_usage")
    """
    return visualize_graph_tool(repo, edge_type)


def main() -> None:
    """Entry point — preload models and start MCP server."""
    start_preload()
    mcp.run(transport="stdio")
