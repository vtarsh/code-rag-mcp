"""Graph MCP tools — find_dependencies, trace_impact, trace_flow, trace_chain.

All graph-based analysis tools registered with FastMCP.
"""

from __future__ import annotations

from src.config import IMPACT_HINTS, KNOWN_FLOWS
from src.container import db_connection, require_db
from src.graph.queries import (
    bfs_chain,
    bfs_dependents,
    find_shortest_paths,
    get_incoming_edges,
    get_outgoing_edges,
    load_flow_edges,
    resolve_repo_name,
)


@require_db
def find_dependencies_tool(repo_name: str) -> str:
    """Find what a repo depends on AND what depends on it.

    Args:
        repo_name: Exact repo name
    """
    with db_connection() as conn:
        resolved, err = resolve_repo_name(conn, repo_name)
        if err:
            return err
        repo_name = resolved  # type: ignore[assignment]

        outgoing = get_outgoing_edges(conn, repo_name)
        incoming = get_incoming_edges(conn, repo_name)

        lines = [f"# Dependencies for {repo_name}\n\n"]

        # Group outgoing by type
        lines.append(f"## This repo depends on ({len(outgoing)} edges):\n")
        by_type: dict[str, list[tuple[str, str]]] = {}
        for e in outgoing:
            by_type.setdefault(e.edge_type, []).append((e.target, e.detail))
        for etype, targets in sorted(by_type.items()):
            lines.append(f"\n**{etype}** ({len(targets)}):\n")
            for target, detail in targets[:20]:
                suffix = f" ({detail})" if detail and detail != target else ""
                lines.append(f"  - {target}{suffix}\n")
            if len(targets) > 20:
                lines.append(f"  ... and {len(targets) - 20} more\n")

        # Group incoming by type
        lines.append(f"\n## Repos depending on this ({len(incoming)} edges):\n")
        by_type = {}
        for e in incoming:
            by_type.setdefault(e.edge_type, []).append((e.source, e.detail))
        for etype, sources in sorted(by_type.items()):
            lines.append(f"\n**{etype}** ({len(sources)}):\n")
            for source, _detail in sources[:20]:
                lines.append(f"  - {source}\n")
            if len(sources) > 20:
                lines.append(f"  ... and {len(sources) - 20} more\n")

        return "".join(lines)


@require_db
def trace_impact_tool(repo_name: str, depth: int = 2) -> str:
    """Trace transitive impact: which repos are affected if this repo changes.

    Args:
        repo_name: Repo to trace impact from (e.g., "providers-proto")
        depth: How many levels deep to trace (default 2, max 4)
    """
    with db_connection() as conn:
        resolved, err = resolve_repo_name(conn, repo_name)
        if err:
            return err
        repo_name = resolved  # type: ignore[assignment]

        depth = min(max(1, depth), 4)
        levels = bfs_dependents(conn, repo_name, depth)

        total_affected = sum(len(v) for v in levels.values())
        lines = [
            f"# Impact Analysis: {repo_name}\n\n",
            f"**Total affected repos**: {total_affected} (depth={depth})\n\n",
        ]

        for level in sorted(levels.keys()):
            repos_at_level = levels[level]
            lines.append(f"## Level {level} — {len(repos_at_level)} repos:\n")

            by_type: dict[str, list[str]] = {}
            for rname, etype in repos_at_level:
                by_type.setdefault(etype, []).append(rname)

            for etype, repos in sorted(by_type.items()):
                lines.append(f"\n**via {etype}** ({len(repos)}):\n")
                for r in sorted(repos)[:30]:
                    lines.append(f"  - {r}\n")
                if len(repos) > 30:
                    lines.append(f"  ... and {len(repos) - 30} more\n")
            lines.append("\n")

        # PR checklist
        if total_affected > 0:
            lines.append("## PR Checklist:\n")
            lines.append(f"- [ ] Changes to {repo_name} are backward-compatible\n")

            affected_names: set[str] = set()
            for repos_list in levels.values():
                for rname, _ in repos_list:
                    affected_names.add(rname)

            for hint_rule in IMPACT_HINTS:
                pfx = hint_rule.get("prefix", "")
                msg = hint_rule.get("hint", "")
                if pfx and msg and any(pfx in r for r in affected_names):
                    lines.append(f"- [ ] {msg}\n")
            if total_affected > 10:
                lines.append("- [ ] Consider phased rollout — many services affected\n")

        return "".join(lines)


@require_db
def trace_flow_tool(source: str, target: str, max_depth: int = 5) -> str:
    """Find the shortest path(s) between two repos/services in the dependency graph.

    Args:
        source: Starting repo (e.g., "express-api-v1")
        target: Destination repo (e.g., "grpc-apm-trustly")
        max_depth: Maximum hops to search (default 5, max 8)
    """
    with db_connection() as conn:
        max_depth = min(max(1, max_depth), 8)

        src, err = resolve_repo_name(conn, source)
        if err:
            return err
        tgt, err = resolve_repo_name(conn, target)
        if err:
            return err
        assert src is not None and tgt is not None

        if src == tgt:
            return f"Source and target are the same: {src}"

        found_paths = find_shortest_paths(conn, src, tgt, max_depth)

    # Score and sort paths
    edge_weight = {
        "grpc_call": 0,
        "grpc_client_usage": 0,
        "webhook_handler": 0,
        "webhook_dispatch": 1,
        "callback_handler": 1,
        "child_workflow": 1,
        "workflow_import": 1,
        "proto_import": 2,
        "npm_dep_proto": 2,
        "npm_dep": 4,
        "npm_dep_tooling": 10,
    }

    def path_score(nodes_edges: tuple[list[str], list[str]]) -> int:
        _, edges_list = nodes_edges
        return sum(edge_weight.get(e.lstrip("←"), 2) for e in edges_list)

    found_paths.sort(key=path_score)

    # Format
    lines = [f"# Flow: {src} → {tgt}\n\n"]

    if not found_paths:
        lines.append(f"**No path found** within {max_depth} hops.\n\n")
        lines.append("This could mean:\n")
        lines.append("- The services are not connected through indexed dependencies\n")
        lines.append("- The connection goes through external systems (HTTP APIs, message queues)\n")
        lines.append(f"- Try increasing max_depth (currently {max_depth})\n")
    else:
        unique_paths: list[tuple[list[str], list[str]]] = []
        seen: set[tuple[str, ...]] = set()
        for nodes, edges in found_paths:
            key = tuple(nodes)
            if key not in seen:
                seen.add(key)
                unique_paths.append((nodes, edges))

        lines.append(f"**{len(unique_paths)} path(s) found** ({len(found_paths[0][0]) - 1} hops):\n\n")

        for i, (nodes, edges) in enumerate(unique_paths[:5], 1):
            lines.append(f"### Path {i}\n```\n")
            for j in range(len(edges)):
                if edges[j].startswith("←"):
                    lines.append(f"  {nodes[j]} ←({edges[j][1:]})— {nodes[j + 1]}\n")
                else:
                    lines.append(f"  {nodes[j]} —({edges[j]})→ {nodes[j + 1]}\n")
            lines.append("```\n\n")

        if len(unique_paths) > 5:
            lines.append(f"... and {len(unique_paths) - 5} more paths\n\n")

        intermediaries = set()
        for nodes, _ in unique_paths:
            for n in nodes[1:-1]:
                intermediaries.add(n)
        if intermediaries:
            lines.append(f"**Key intermediaries**: {', '.join(sorted(intermediaries))}\n")

    return "".join(lines)


@require_db
def trace_chain_tool(start: str, direction: str = "both", max_depth: int = 4) -> str:
    """Trace the processing chain through services starting from a repo or concept.

    Args:
        start: A repo name OR a concept name (payment, settlement, dispute, 3ds, risk, auth, reconciliation, webhook)
        direction: "downstream" (what it calls), "upstream" (who calls it), or "both" (default)
        max_depth: How many hops to follow (default 4, max 6)
    """
    with db_connection() as conn:
        max_depth = min(max(1, max_depth), 6)

        # Resolve start
        start_lower = start.lower().strip()
        concept: str | None = None
        if start_lower in KNOWN_FLOWS:
            seed_nodes: list[str] = []
            for candidate in KNOWN_FLOWS[start_lower]:
                row = conn.execute("SELECT name FROM graph_nodes WHERE name = ?", (candidate,)).fetchone()
                if row:
                    seed_nodes.append(row["name"])
            if not seed_nodes:
                return f"Known flow '{start}' has no matching repos in the graph."
            concept = start_lower
        else:
            row = conn.execute("SELECT name FROM graph_nodes WHERE name = ?", (start,)).fetchone()
            if row:
                seed_nodes = [row["name"]]
            else:
                candidates = conn.execute(
                    "SELECT name FROM graph_nodes WHERE name LIKE ? LIMIT 10", (f"%{start}%",)
                ).fetchall()
                if len(candidates) == 1:
                    seed_nodes = [candidates[0]["name"]]
                elif candidates:
                    return f"'{start}' is ambiguous. Did you mean: {', '.join(c['name'] for c in candidates)}"
                else:
                    return f"'{start}' not found in graph. Try a concept: {', '.join(sorted(KNOWN_FLOWS.keys()))}"

        # Load edges and build adjacency
        edges = load_flow_edges(conn)
        downstream_adj: dict[str, list[tuple[str, str, str]]] = {}
        upstream_adj: dict[str, list[tuple[str, str, str]]] = {}
        for e in edges:
            downstream_adj.setdefault(e["source"], []).append((e["target"], e["edge_type"], e.get("detail", "")))
            upstream_adj.setdefault(e["target"], []).append((e["source"], e["edge_type"], e.get("detail", "")))

    # BFS
    show_downstream = direction in ("downstream", "both")
    show_upstream = direction in ("upstream", "both")
    downstream_tree = bfs_chain(seed_nodes, downstream_adj, max_depth) if show_downstream else {}
    upstream_tree = bfs_chain(seed_nodes, upstream_adj, max_depth) if show_upstream else {}

    # Format
    title = concept.upper() if concept else ", ".join(seed_nodes)
    lines = [f"# Chain: {title}\n\nStarting from: {', '.join(seed_nodes)}\n\n"]

    def format_tree(
        tree: dict[str, tuple[int, str | None, str | None]],
        seeds: set[str],
        label: str,
        direction_arrow: str,
    ) -> str:
        if len(tree) <= len(seeds):
            return f"### {label}\nNo connections found.\n\n"

        by_depth: dict[int, list[tuple[str, str | None, str | None]]] = {}
        for node, (depth, parent, etype) in tree.items():
            if node not in seeds:
                by_depth.setdefault(depth, []).append((node, parent, etype))

        parts = [f"### {label} ({sum(len(v) for v in by_depth.values())} services)\n\n```\n"]
        for depth in sorted(by_depth.keys()):
            for node, parent, etype in sorted(by_depth[depth], key=lambda x: x[0]):
                indent = "  " * depth
                if direction_arrow == "→":
                    parts.append(f"{indent}{parent} —({etype})→ {node}\n")
                else:
                    parts.append(f"{indent}{node} —({etype})→ {parent}\n")
        parts.append("```\n\n")
        return "".join(parts)

    if show_downstream:
        lines.append(format_tree(downstream_tree, set(seed_nodes), "Downstream (what it calls)", "→"))
    if show_upstream:
        lines.append(format_tree(upstream_tree, set(seed_nodes), "Upstream (who calls it)", "←"))

    # Summary with clusters
    all_services = set(downstream_tree.keys()) | set(upstream_tree.keys()) - set(seed_nodes)
    if all_services:
        try:
            from scripts.visualize_graph import get_cluster

            clusters: dict[str, list[str]] = {}
            for s in all_services:
                c = get_cluster(s)
                clusters.setdefault(c, []).append(s)

            lines.append("### Summary\n")
            lines.append(f"**Total services in chain**: {len(all_services)}\n\n")
            lines.append("| Cluster | Count | Services |\n|---------|-------|----------|\n")
            for cluster in sorted(clusters.keys(), key=lambda k: -len(clusters[k])):
                services = sorted(clusters[cluster])
                display = ", ".join(services[:5])
                if len(services) > 5:
                    display += f" ... (+{len(services) - 5})"
                lines.append(f"| {cluster} | {len(services)} | {display} |\n")
        except ImportError:
            lines.append(f"### Summary\n**Total services in chain**: {len(all_services)}\n")

    return "".join(lines)
