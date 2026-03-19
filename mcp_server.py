#!/usr/bin/env python3
"""Code Knowledge Base — MCP Server (thin proxy).

Lightweight stdio MCP server that proxies all tool calls to the persistent
daemon process via HTTP. This avoids loading ~1.4 GB of ML models per session.

The daemon (daemon.py) must be running on localhost:8742.
If it's not running, this proxy will attempt to start it automatically.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# Auto-detect CODE_RAG_HOME from script location (mcp_server lives in project root)
if "CODE_RAG_HOME" not in os.environ:
    os.environ["CODE_RAG_HOME"] = str(Path(__file__).resolve().parent)

from mcp.server.fastmcp import FastMCP

DAEMON_PORT = int(os.environ.get("CODE_RAG_PORT", os.environ.get("PAY_KNOWLEDGE_PORT", "8742")))
DAEMON_URL = f"http://127.0.0.1:{DAEMON_PORT}"
PROJECT_DIR = Path(__file__).parent
PID_FILE = PROJECT_DIR / "daemon.pid"

mcp = FastMCP("code-rag")


def _daemon_healthy() -> bool:
    """Check if daemon is responding."""
    try:
        req = Request(f"{DAEMON_URL}/health", method="GET")
        with urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except (URLError, OSError, TimeoutError):
        return False


def _start_daemon() -> bool:
    """Start daemon as a detached background process. Returns True if started."""
    # Check if PID file points to a live process
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # check if alive
            # Process exists but not responding — kill stale
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        PID_FILE.unlink(missing_ok=True)

    # Start daemon
    daemon_script = str(PROJECT_DIR / "daemon.py")
    subprocess.Popen(
        [sys.executable, daemon_script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    # Wait for daemon to be ready (up to 30s for model loading)
    for _ in range(60):
        time.sleep(0.5)
        if _daemon_healthy():
            return True
    return False


def _ensure_daemon() -> str | None:
    """Ensure daemon is running. Returns error string or None."""
    if _daemon_healthy():
        return None
    if _start_daemon():
        return None
    return "Failed to start knowledge base daemon. Run manually: python3 ~/.code-rag/daemon.py"


def _call_daemon(tool_name: str, args: dict) -> str:
    """Call a tool on the daemon via HTTP POST. Returns result string."""
    err = _ensure_daemon()
    if err:
        return err

    body = json.dumps(args).encode()
    req = Request(
        f"{DAEMON_URL}/tool/{tool_name}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            if "error" in data:
                return f"Error: {data['error']}"
            return data.get("result", "")
    except URLError as e:
        return f"Daemon connection error: {e}"
    except TimeoutError:
        return "Daemon request timed out (120s)"


# --- MCP Tools (all proxy to daemon) ---


@mcp.tool()
def search(query: str, repo: str = "", file_type: str = "", limit: int = 10) -> str:
    """Search the knowledge base using keyword + semantic hybrid search.

    Works for both keyword queries ("settlement account", "webhook callback")
    and natural language questions ("which services handle payment settlements",
    "how does 3DS authentication work"). Uses hybrid search (FTS5 + vector + reranker)
    internally to find the best results regardless of query style.

    Args:
        query: Search query — keywords or natural language question
        repo: Optional - filter by repo name (exact or partial match)
        file_type: Optional - filter by type: proto, docs, config, env, k8s, grpc_method, library, workflow, ci, gotchas
        limit: Max results to return (default 10, max 20)
    """
    return _call_daemon("search", {"query": query, "repo": repo, "file_type": file_type, "limit": limit})


@mcp.tool()
def find_dependencies(repo_name: str) -> str:
    """Find what a repo depends on AND what depends on it.

    Args:
        repo_name: Exact repo name
    """
    return _call_daemon("find_dependencies", {"repo_name": repo_name})


@mcp.tool()
def trace_impact(repo_name: str, depth: int = 2) -> str:
    """Trace transitive impact: which repos are affected if this repo changes.

    Uses the dependency graph to find all repos that directly or transitively
    depend on the given repo. Essential for "what might I miss" analysis before PRs.

    Args:
        repo_name: Repo to trace impact from (e.g., "providers-proto", "node-libs-types")
        depth: How many levels deep to trace (default 2, max 4)
    """
    return _call_daemon("trace_impact", {"repo_name": repo_name, "depth": depth})


@mcp.tool()
def trace_flow(source: str, target: str, max_depth: int = 5) -> str:
    """Find the shortest path(s) between two repos/services in the dependency graph.

    Answers questions like "how does express-api-v1 connect to grpc-apm-trustly?"

    Args:
        source: Starting repo (e.g., "express-api-v1", "grpc-payment-gateway")
        target: Destination repo (e.g., "grpc-apm-trustly", "workflow-settlement-worker")
        max_depth: Maximum hops to search (default 5, max 8)
    """
    return _call_daemon("trace_flow", {"source": source, "target": target, "max_depth": max_depth})


@mcp.tool()
def trace_chain(start: str, direction: str = "both", max_depth: int = 4) -> str:
    """Trace the processing chain through services starting from a repo or concept.

    Unlike trace_flow (which finds A→B paths), this explores the full call chain
    from a starting point — upstream (who calls it) and downstream (what it calls).

    Args:
        start: A repo name OR a concept name (payment, settlement, dispute, 3ds, risk, auth, reconciliation, webhook)
        direction: "downstream" (what it calls), "upstream" (who calls it), or "both" (default)
        max_depth: How many hops to follow (default 4, max 6)
    """
    return _call_daemon("trace_chain", {"start": start, "direction": direction, "max_depth": max_depth})


@mcp.tool()
def repo_overview(repo_name: str) -> str:
    """Get detailed overview of a specific repo.

    Args:
        repo_name: Exact repo name (e.g., "grpc-apm-trustly", "workflow-provider-webhooks")
    """
    return _call_daemon("repo_overview", {"repo_name": repo_name})


@mcp.tool()
def list_repos(type: str = "", has_dep: str = "", limit: int = 30) -> str:
    """List repos filtered by type or dependency.

    Args:
        type: Filter by repo type: grpc-service-js, grpc-service-ts, temporal-workflow, library, boilerplate, node-service, ci-actions, gitops
        has_dep: Filter repos that depend on this package (e.g., "providers-proto", "types", "temporal")
        limit: Max results (default 30)
    """
    return _call_daemon("list_repos", {"type": type, "has_dep": has_dep, "limit": limit})


@mcp.tool()
def analyze_task(description: str, provider: str = "") -> str:
    """Analyze a development task and find ALL relevant repos, files, and dependencies.

    Takes a task description (e.g., "add verification flow to Trustly") and automatically:
    1. Identifies relevant provider repo, webhook activities, gateway methods
    2. Checks proto contracts for required methods
    3. Traces the dependency graph for affected repos
    4. Searches GitHub for existing PRs/branches related to this task
    5. Generates a completeness report and change checklist

    Args:
        description: Task description (e.g., "implement DirectDebitMandate verification for Trustly")
        provider: Optional provider name to focus on (e.g., "trustly", "paypal")
    """
    return _call_daemon("analyze_task", {"description": description, "provider": provider})


@mcp.tool()
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
        query: What you're working on (e.g., "add refund support to Trustly", "settlement reconciliation flow")
        repo: Optional — focus on a specific repo
        include_deps: Include dependency graph for discovered repos (default: true)
        include_proto: Include proto definitions if found (default: true)
        search_limit: Max search results (default 8)
    """
    return _call_daemon(
        "context_builder",
        {
            "query": query,
            "repo": repo,
            "include_deps": include_deps,
            "include_proto": include_proto,
            "search_limit": search_limit,
        },
    )


@mcp.tool()
def health_check() -> str:
    """Return a diagnostic report on the knowledge base: database, vector store,
    models, graph, and consistency status. Takes no arguments."""
    return _call_daemon("health_check", {})


@mcp.tool()
def visualize_graph(repo: str = "", edge_type: str = "") -> str:
    """Generate an interactive D3.js graph visualization and open it in the browser.

    Args:
        repo: Optional — focus on a specific repo's neighborhood (e.g., "grpc-apm-trustly")
        edge_type: Optional — show only a specific edge type (e.g., "grpc_call", "grpc_client_usage")
    """
    return _call_daemon("visualize_graph", {"repo": repo, "edge_type": edge_type})


def main() -> None:
    """Entry point — start thin MCP proxy (no model loading)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
