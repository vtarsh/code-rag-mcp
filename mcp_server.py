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
# Remote mode: point CODE_RAG_HOST at a teammate's daemon (VPN/Tailscale IP).
# With a remote host this proxy never tries to start a local daemon.
DAEMON_HOST = os.environ.get("CODE_RAG_HOST", "127.0.0.1")
DAEMON_URL = f"http://{DAEMON_HOST}:{DAEMON_PORT}"
IS_REMOTE = DAEMON_HOST not in ("127.0.0.1", "localhost")
# Shared secret matching the daemon's CODE_RAG_TOKEN (required when the daemon sets one).
AUTH_TOKEN = os.environ.get("CODE_RAG_TOKEN", "")
PROJECT_DIR = Path(__file__).parent
PID_FILE = PROJECT_DIR / "daemon.pid"

mcp = FastMCP("code-rag")

_SESSION_ID = f"mcp-{os.getpid()}-{int(time.time())}"


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
    if IS_REMOTE:
        return f"Remote knowledge base daemon at {DAEMON_URL} is not reachable (check VPN/host/port)."
    if _start_daemon():
        return None
    return "Failed to start knowledge base daemon. Run manually: python3 ~/.code-rag/daemon.py"


def _call_daemon(tool_name: str, args: dict) -> str:
    """Call a tool on the daemon via HTTP POST. Returns result string."""
    err = _ensure_daemon()
    if err:
        return err

    body = json.dumps(args).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "mcp_server", "X-Session-ID": _SESSION_ID}
    if AUTH_TOKEN:
        headers["X-Auth-Token"] = AUTH_TOKEN
    req = Request(
        f"{DAEMON_URL}/tool/{tool_name}",
        data=body,
        headers=headers,
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
def search(
    query: str,
    repo: str = "",
    file_type: str = "",
    exclude_file_types: str = "",
    limit: int = 10,
    brief: bool = False,
    cross_provider: bool = False,
) -> str:
    """Search the knowledge base using keyword + semantic hybrid search.

    Works for both keyword queries ("settlement account", "webhook callback")
    and natural language questions ("which services handle payment settlements",
    "how does 3DS authentication work"). Uses hybrid search (FTS5 + vector + reranker)
    internally to find the best results regardless of query style.

    Exact symbols are safe here: when the query names a code identifier or
    filename (getSepaSyncResponse, SOURCE_TYPES, map-response.js) that the
    semantic index ranks out, search() automatically folds in the literal
    source matches from the shadow clones under a "📁 Literal source matches"
    block — so a single call is as strong as grepping the repo by hand. If even
    that is empty, the output points you at grep_shadow for a manual sweep.

    Args:
        query: Search query — keywords or natural language question
        repo: Optional - filter by repo name (exact or partial match)
        file_type: Optional - filter by type: proto, docs, config, env, k8s, grpc_method, library, workflow, ci, gotchas
        exclude_file_types: Optional - comma-separated file types to exclude from results (e.g. "gotchas,task")
        limit: Max results to return (default 10, max 50). When the limit is
            reached and more candidates exist, the output ends with a coverage
            hint — re-run with a higher limit for broad/multi-file tasks.
        brief: When True, drop "Found N of M candidates for 'query'" header
            (re-echoes query), strip >>><<< highlight markers, and drop
            [keyword+vector] source tags. Default False preserves current output.
        cross_provider: When True and query matches {provider} {operation} pattern,
            also returns top-1 analogous chunk from up to 6 sibling providers —
            eliminates provider-swap reformulation chains.
    """
    return _call_daemon(
        "search",
        {
            "query": query,
            "repo": repo,
            "file_type": file_type,
            "exclude_file_types": exclude_file_types,
            "limit": limit,
            "brief": brief,
            "cross_provider": cross_provider,
        },
    )


@mcp.tool()
def trace_impact(repo_name: str, max_depth: int = 2) -> str:
    """Trace transitive impact: which repos depend on this one (transitively).

    For "what breaks if I change repo X" analysis before PRs. Also replaces the
    previous find_dependencies tool — use max_depth=1 for direct dependencies.

    Args:
        repo_name: Repo to trace impact from (e.g., "providers-proto", "node-libs-types")
        max_depth: How many levels deep to trace (default 2, max 4). Use 1 for direct deps only.
    """
    return _call_daemon("trace_impact", {"repo_name": repo_name, "max_depth": max_depth})


@mcp.tool()
def trace_flow(source: str, target: str, max_depth: int = 5) -> str:
    """Find the shortest dependency path(s) between two repos/services.

    Answers "how does express-api-v1 connect to grpc-apm-trustly?". Path-finding is
    UNDIRECTED for reachability — the arrows in the output show the true edge
    direction (A —(grpc_client_usage)→ B), but a path may traverse edges against
    their arrows, so source and target are interchangeable. Unknown repo → "'<name>'
    not found in graph."; a valid but unconnected pair → a clear "no path" message.

    Args:
        source: Starting repo (e.g., "express-api-v1", "grpc-payment-gateway")
        target: Destination repo (e.g., "grpc-apm-trustly", "workflow-settlement-worker")
        max_depth: Maximum hops to search (default 5, max 8)

    Example: trace_flow(source="grpc-payment-gateway", target="grpc-apm-trustly")
    """
    return _call_daemon("trace_flow", {"source": source, "target": target, "max_depth": max_depth})


@mcp.tool()
def trace_chain(start: str, direction: str = "both", max_depth: int = 4) -> str:
    """Trace the processing chain through services starting from a repo or concept.

    Unlike trace_flow (which finds A→B paths), this explores the full call chain
    from a starting point — upstream (who calls it) and downstream (what it calls).

    Args:
        start: An exact repo name (e.g. "grpc-apm-volt") OR one of the supported
            concepts: 3ds, apm, auth, cdc, clickhouse, data-ingestion, dispute,
            onboarding, payment, reconciliation, risk, settlement, verification,
            webhook. An unknown start returns a friendly "not found" listing valid
            concepts.
        direction: Exactly one of "downstream" (what it calls), "upstream" (who
            calls it), or "both" (default). Any other value yields an empty chain.
        max_depth: How many hops to follow (default 4, max 6)

    Example: trace_chain(start="grpc-apm-volt", direction="downstream", max_depth=4)
    """
    return _call_daemon("trace_chain", {"start": start, "direction": direction, "max_depth": max_depth})


@mcp.tool()
def trace_field(field: str, provider: str = "", mode: str = "trace") -> str:
    """Trace a field through the provider→gateway→webhook service chain.

    Core principle: every field change must be traced through ALL services. A field
    with no chain returns a graceful "No trace chain found." (not an error).

    Args:
        field: Field name, optionally dotted to a producer
            (e.g., "processorTransactionId", "finalize.issuerResponseCode")
        provider: Optional provider filter (e.g., "payper", "volt", "trustly")
        mode: "trace" (full producer→consumer chain, default), "consumers" (only
            services that READ it — best with provider=), "compare" (how it differs
            across providers), "contract" (curated field spec; falls back to a code
            grep when no curated contract exists).

    Examples: trace_field(field="processorTransactionId", mode="trace");
        trace_field(field="processorTransactionId", provider="payper", mode="consumers").
    NOTE: for a very common field, "contract" can return a large grep fallback —
    pass provider= to scope it.
    """
    return _call_daemon("trace_field", {"field": field, "provider": provider, "mode": mode})


@mcp.tool()
def repo_overview(repo_name: str) -> str:
    """Get detailed overview of a specific repo.

    Args:
        repo_name: Exact repo name (e.g., "grpc-apm-trustly", "workflow-provider-webhooks")
    """
    return _call_daemon("repo_overview", {"repo_name": repo_name})


@mcp.tool()
def list_repos(type: str = "", has_dep: str = "", limit: int = 30, include_deps: bool = False) -> str:
    """List repos filtered by type and/or dependency.

    Returns a "Found N repos ... (showing M)" header then "**repo-name** (type)"
    rows. An unknown type returns a friendly "No repos found matching criteria.",
    not an error.

    Args:
        type: Repo type — grpc-service-js, grpc-service-ts, temporal-workflow, library, boilerplate, node-service, ci-actions, gitops
        has_dep: Filter to repos that depend on this package (e.g., "providers-proto", "types", "temporal"); auto-appends "— N org deps" to each row
        limit: Max results (default 30)
        include_deps: Append the "— N org deps" suffix. NOTE: the suffix reliably
            appears only when has_dep is set; include_deps alone may not add it.

    Examples: list_repos(type="grpc-service-js", limit=10);
        list_repos(has_dep="providers-proto", limit=8).
    """
    return _call_daemon(
        "list_repos",
        {"type": type, "has_dep": has_dep, "limit": limit, "include_deps": include_deps},
    )


@mcp.tool()
def analyze_task(description: str, provider: str = "", exclude_task_id: str = "", brief: bool = False) -> str:
    """FIRST TOOL for any review/audit/investigation task. Returns relevant repos, files, dependencies, PRs, and a top-of-output SHARED FILE IMPACT warning that names cross-provider consumers of changed files.

    Use for review ("did we break X?"), new-feature scoping, and bug investigation.
    Fast (~3-5s). Read the SHARED FILE IMPACT / REVIEW MODE sections at the top of
    the output before anything else.

    Args:
        description: Task/review prompt verbatim. The more specific, the tighter
            the result — a vague "fix bug" falls back to an all-provider fan-out.
        provider: Optional provider to focus on (e.g., "trustly", "volt", "nuvei")
        exclude_task_id: Optional task ID to exclude from task_history — blind eval only.
        brief: When True, drop repeated preamble/disclaimer prose; section headers
            and body are preserved. The size win is most visible on long verbose
            outputs (not a fixed %).

    Example: analyze_task(description="Investigate Volt webhook signature", provider="volt", brief=True)
    """
    return _call_daemon(
        "analyze_task",
        {
            "description": description,
            "provider": provider,
            "exclude_task_id": exclude_task_id,
            "brief": brief,
        },
    )


@mcp.tool()
def health_check() -> str:
    """Return a diagnostic report on the knowledge base: database, vector store,
    models, graph, and consistency status. Takes no arguments."""
    return _call_daemon("health_check", {})


@mcp.tool()
def trace_internal(repo_name: str, method: str = "") -> str:
    """Trace intra-service require() call chain within a provider repo.

    Shows the file-level execution path: methods/sale.js → libs/map-request.js → libs/statuses-map.js.
    Unlike trace_chain (repo-to-repo), this traces file-to-file WITHIN a single service.
    Unknown repo/method returns a friendly "No internal traces found" message.
    Backing table is built from raw/ via `python3 scripts/build/build_internal_traces.py`
    (covers grpc-apm-* / grpc-providers-* methods).

    Args:
        repo_name: Provider repo name (e.g., "grpc-apm-payper", "grpc-providers-nuvei")
        method: Optional — specific method to trace (e.g., "sale", "refund"). If empty, shows all methods.

    Example: trace_internal(repo_name="grpc-apm-payper", method="sale")
    """
    return _call_daemon("trace_internal", {"repo_name": repo_name, "method": method})


@mcp.tool()
def provider_type_map(provider: str, method: str = "", mode: str = "overview") -> str:
    """Show shadow type map for a provider — proto-to-JS field mappings, API endpoints, and type gaps.

    Shows how fields flow from gateway proto messages through provider JS code
    to external API payloads and back. Identifies type gaps where fields lose typing.

    Build maps first: python scripts/build_shadow_types.py --provider=payper

    Args:
        provider: Provider name (e.g., "payper")
        method: Specific method (e.g., "initialize", "sale", "refund"). Required for "fields" mode.
        mode: "overview" (chain summary), "fields" (field-level detail), "gaps" (type gap report)
    """
    return _call_daemon("provider_type_map", {"provider": provider, "method": method, "mode": mode})


@mcp.tool()
def grep_shadow(
    pattern: str,
    repo: str = "",
    glob: str = "",
    max_results: int = 100,
    context: int = 0,
    case_insensitive: bool = False,
    fixed_string: bool = False,
) -> str:
    """Grep full source in the shadow clones of ALL indexed org repos — no local clone needed.

    Two jobs: (1) the RECALL FALLBACK when search() comes back thin or empty —
    full-text grep finds exact symbols, constants, and filenames the semantic
    index ranks out (search() already auto-folds the obvious ones, but reach
    here directly to sweep wider, scope a repo, or use a regex); (2) verifying
    the actual logic around a hit — search finds the place, grep_shadow +
    read_shadow_file confirm the code. Prefer this over telling the user "the
    knowledge base doesn't have it": if it's in the source, grep finds it.
    The shadow is a snapshot of each repo's default branch from the last index
    build (see health_check) — feature branches and unpushed changes are absent.

    Args:
        pattern: Regex (ERE), or literal text when fixed_string=True
        repo: Optional repo name to scope the search (e.g., "grpc-apm-volt")
        glob: Optional filename filter (e.g., "*.js", "initialize*")
        max_results: Cap on emitted match lines (default 100, max 500)
        context: Lines of context around each match, 0-5 (grep -C)
        case_insensitive: Case-insensitive match
        fixed_string: Treat pattern as a literal string instead of a regex
    """
    return _call_daemon(
        "grep_shadow",
        {
            "pattern": pattern,
            "repo": repo,
            "glob": glob,
            "max_results": max_results,
            "context": context,
            "case_insensitive": case_insensitive,
            "fixed_string": fixed_string,
        },
    )


@mcp.tool()
def read_shadow_file(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a full source file from the shadow clone, with line numbers.

    Companion to grep_shadow: grep finds `repo/path:line`, this reads the file
    around it. Snapshot of the default branch from the last index build.

    Args:
        path: Repo-relative path, e.g. "grpc-apm-volt/methods/initialize.js"
        offset: 1-based first line to return (default 1)
        limit: Max lines to return (default 200, max 1000)
    """
    return _call_daemon("read_shadow_file", {"path": path, "offset": offset, "limit": limit})


@mcp.tool()
def list_shadow_dir(path: str = "") -> str:
    """List a directory inside the shadow clone (dirs first, then files with sizes).

    A FILE path returns a hint to use read_shadow_file; an unknown repo/path returns
    a friendly "not found" suggesting list_shadow_dir(""). Large listings cap at the
    first 500 entries.

    Args:
        path: Repo-relative dir, e.g. "grpc-apm-volt/methods". Empty lists all repos.

    Examples: list_shadow_dir("") to see all repos; list_shadow_dir("grpc-apm-volt")
        for a repo's top-level dirs; list_shadow_dir("grpc-apm-volt/methods") for files.
    """
    return _call_daemon("list_shadow_dir", {"path": path})


def main() -> None:
    """Entry point — start thin MCP proxy (no model loading)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
