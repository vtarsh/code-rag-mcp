"""Graph database queries and BFS traversal utilities.

Low-level functions for querying graph_nodes and graph_edges tables.
Used by graph service tools (trace_impact, trace_flow, trace_chain, find_dependencies).
"""

from __future__ import annotations

import sqlite3
from collections import deque

from src.config import FLOW_EDGE_TYPES
from src.types import GraphEdge


def resolve_repo_name(conn: sqlite3.Connection, name: str) -> tuple[str | None, str | None]:
    """Resolve a repo name (exact or partial match).

    Returns (resolved_name, error_message).
    """
    exact = conn.execute("SELECT name FROM graph_nodes WHERE name = ?", (name,)).fetchone()
    if exact:
        return exact["name"], None

    candidates = conn.execute("SELECT name FROM graph_nodes WHERE name LIKE ? LIMIT 10", (f"%{name}%",)).fetchall()
    if len(candidates) == 1:
        return candidates[0]["name"], None
    if candidates:
        return None, f"'{name}' is ambiguous. Did you mean: {', '.join(c['name'] for c in candidates)}"
    return None, f"'{name}' not found in graph."


def get_outgoing_edges(conn: sqlite3.Connection, repo_name: str) -> list[GraphEdge]:
    """Get all outgoing edges from a repo."""
    rows = conn.execute(
        "SELECT source, target, edge_type, detail FROM graph_edges WHERE source = ? ORDER BY edge_type, target",
        (repo_name,),
    ).fetchall()
    return [
        GraphEdge(source=r["source"], target=r["target"], edge_type=r["edge_type"], detail=r["detail"] or "")
        for r in rows
    ]


def get_incoming_edges(conn: sqlite3.Connection, repo_name: str) -> list[GraphEdge]:
    """Get all incoming edges to a repo."""
    rows = conn.execute(
        "SELECT source, target, edge_type, detail FROM graph_edges WHERE target = ? ORDER BY edge_type, source",
        (repo_name,),
    ).fetchall()
    return [
        GraphEdge(source=r["source"], target=r["target"], edge_type=r["edge_type"], detail=r["detail"] or "")
        for r in rows
    ]


def bfs_dependents(
    conn: sqlite3.Connection,
    start: str,
    max_depth: int = 2,
    max_in_degree: int = 0,
) -> dict[int, list[tuple[str, str]]]:
    """BFS to find all repos that transitively depend on start.

    Args:
        max_in_degree: Hub penalty threshold. If > 0, nodes whose in-degree
            (number of distinct dependents) exceeds this value are still added
            to the results but NOT expanded further. This prevents ultra-high-
            degree hubs (e.g., libs-types with 400+ dependents) from flooding
            the BFS with the entire org.

    Returns {level: [(repo_name, via_edge_type)]}.
    """
    # Pre-compute hub nodes when hub penalty is active
    hub_nodes: set[str] = set()
    if max_in_degree > 0:
        hub_rows = conn.execute(
            """SELECT target, COUNT(DISTINCT source) as cnt FROM graph_edges
               WHERE source NOT LIKE 'pkg:%' AND source NOT LIKE 'proto:%'
               AND source NOT LIKE 'route:%'
               GROUP BY target HAVING cnt > ?""",
            (max_in_degree,),
        ).fetchall()
        hub_nodes = {r["target"] for r in hub_rows}

    visited: set[str] = set()
    levels: dict[int, list[tuple[str, str]]] = {}
    queue: deque[tuple[str, int, str]] = deque([(start, 0, "root")])

    while queue:
        current, level, via_type = queue.popleft()
        if current in visited or level > max_depth:
            continue
        visited.add(current)
        if current != start:
            levels.setdefault(level, []).append((current, via_type))

        # Hub penalty: add hub to results but don't expand its dependents
        if current in hub_nodes and current != start:
            continue

        if level < max_depth:
            dependents = conn.execute(
                """SELECT DISTINCT source, edge_type FROM graph_edges
                   WHERE target = ? AND source NOT LIKE 'pkg:%' AND source NOT LIKE 'proto:%'
                   AND source NOT LIKE 'route:%'""",
                (current,),
            ).fetchall()
            for dep in dependents:
                if dep["source"] not in visited:
                    queue.append((dep["source"], level + 1, dep["edge_type"]))

    return levels


def load_flow_edges(conn: sqlite3.Connection) -> list[dict]:
    """Load meaningful edges for flow tracing (skip npm_dep, tooling, virtual nodes)."""
    placeholders = ",".join("?" for _ in FLOW_EDGE_TYPES)
    rows = conn.execute(
        f"""SELECT source, target, edge_type, detail FROM graph_edges
            WHERE edge_type IN ({placeholders})
            AND source NOT LIKE 'pkg:%' AND target NOT LIKE 'pkg:%'
            AND source NOT LIKE 'msg:%' AND target NOT LIKE 'msg:%'
            AND source NOT LIKE 'svc:%' AND target NOT LIKE 'svc:%'""",
        list(FLOW_EDGE_TYPES),
    ).fetchall()
    return [dict(r) for r in rows]


def bfs_chain(
    seeds: list[str],
    adj: dict[str, list[tuple[str, str, str]]],
    depth_limit: int,
) -> dict[str, tuple[int, str | None, str | None]]:
    """BFS from seeds using adjacency list.

    Returns {node: (depth, parent, edge_type)}.
    """
    visited: dict[str, tuple[int, str | None, str | None]] = {}
    queue: deque[tuple[str, int]] = deque()
    for s in seeds:
        visited[s] = (0, None, None)
        queue.append((s, 0))
    while queue:
        node, depth = queue.popleft()
        if depth >= depth_limit:
            continue
        for neighbor, etype, _detail in adj.get(node, []):
            if neighbor.startswith(("pkg:", "proto:", "workflow:", "msg:", "svc:", "route:")):
                continue
            if neighbor not in visited:
                visited[neighbor] = (depth + 1, node, etype)
                queue.append((neighbor, depth + 1))
    return visited


def find_shortest_paths(
    conn: sqlite3.Connection,
    src: str,
    tgt: str,
    max_depth: int = 5,
) -> list[tuple[list[str], list[str]]]:
    """BFS for all shortest paths between src and tgt.

    Builds bidirectional adjacency. Hub nodes (>100 incoming edges) are
    penalized with +1 depth cost but NEVER excluded — they may be the
    only path between two services. Skips npm_dep_tooling edges.

    Returns list of (node_path, edge_path).
    """
    # Identify hub nodes — penalize but don't exclude
    hub_threshold = 100
    hub_nodes = set(
        r["target"]
        for r in conn.execute(
            """SELECT target, COUNT(*) as cnt FROM graph_edges
               WHERE source NOT LIKE 'pkg:%' AND target NOT LIKE 'pkg:%'
               GROUP BY target HAVING cnt > ?""",
            (hub_threshold,),
        ).fetchall()
    )
    hub_nodes.discard(src)
    hub_nodes.discard(tgt)

    placeholders = ",".join("?" for _ in FLOW_EDGE_TYPES)
    edges = conn.execute(
        f"""SELECT source, target, edge_type FROM graph_edges
            WHERE edge_type IN ({placeholders})
            AND source NOT LIKE 'pkg:%' AND target NOT LIKE 'pkg:%'
            AND source NOT LIKE 'msg:%' AND target NOT LIKE 'msg:%'
            AND source NOT LIKE 'svc:%' AND target NOT LIKE 'svc:%'""",
        list(FLOW_EDGE_TYPES),
    ).fetchall()

    # Bidirectional adjacency — all nodes included
    fwd: dict[str, list[tuple[str, str]]] = {}
    for e in edges:
        fwd.setdefault(e["source"], []).append((e["target"], e["edge_type"]))
        fwd.setdefault(e["target"], []).append((e["source"], f"←{e['edge_type']}"))

    # Weighted BFS (Dijkstra-lite): hub nodes cost 2, normal nodes cost 1
    # Uses (cost, path) so non-hub paths are preferred when equal length
    queue: deque[tuple[str, list[str], list[str], int]] = deque([(src, [src], [], 0)])
    visited_at_cost: dict[str, int] = {src: 0}
    found_paths: list[tuple[list[str], list[str]]] = []
    found_cost: int | None = None

    while queue:
        current, path_nodes, path_edges, cost = queue.popleft()

        if found_cost is not None and cost > found_cost:
            break
        if cost >= max_depth:
            continue

        for neighbor, edge_type in fwd.get(current, []):
            if neighbor in set(path_nodes):
                continue

            # Hub nodes cost +2 to traverse, normal nodes +1
            step_cost = 2 if neighbor in hub_nodes else 1
            new_cost = cost + step_cost
            new_nodes = [*path_nodes, neighbor]
            new_edges = [*path_edges, edge_type]

            if neighbor == tgt:
                found_paths.append((new_nodes, new_edges))
                found_cost = new_cost
                continue

            if new_cost >= max_depth:
                continue

            prev_cost = visited_at_cost.get(neighbor)
            if prev_cost is not None and prev_cost < new_cost:
                continue

            visited_at_cost[neighbor] = new_cost
            queue.append((neighbor, new_nodes, new_edges, new_cost))

    return found_paths
