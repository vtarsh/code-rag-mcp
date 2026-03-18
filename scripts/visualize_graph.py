#!/usr/bin/env python3
"""
Generate an interactive Sigma.js WebGL graph visualization
of the service dependency graph.

Usage:
  python3 visualize_graph.py [--open]              # all edges (filtered)
  python3 visualize_graph.py --repo=express-api-v1  # subgraph around a repo
  python3 visualize_graph.py --type=grpc_call        # only specific edge type
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = BASE_DIR / "db" / "knowledge.db"
OUTPUT_PATH = BASE_DIR / "graph.html"

# Edge types that are too noisy to show by default
NOISY_EDGE_TYPES = {"npm_dep_tooling", "proto_message_def", "proto_service_def"}

# Hub nodes with many edges that clutter the graph
HUB_THRESHOLD = 150

# Domain clustering rules: (cluster_name, list_of_prefixes)
CLUSTER_RULES = [
    ("Providers", ["grpc-apm-", "grpc-providers-"]),
    ("Core", ["grpc-core-"]),
    ("Workflows", ["workflow-"]),
    ("APIs", ["express-"]),
    ("Node Services", ["node-"]),
    ("Auth", ["grpc-auth-", "grpc-oauth-"]),
    ("Risk", ["grpc-risk-"]),
    ("Vault/Security", ["grpc-vault-"]),
    ("Infra", ["grpc-envoy-", "grpc-utils-", "cloudflare-"]),
    ("Frontend", ["next-"]),
    ("3DS/MPI", ["grpc-mpi-"]),
    ("Payments", ["grpc-payment-", "grpc-settlement-", "grpc-webhooks-"]),
]


def get_cluster(name: str) -> str:
    """Determine which cluster a node belongs to based on name prefix."""
    for cluster_name, prefixes in CLUSTER_RULES:
        for prefix in prefixes:
            if name.startswith(prefix):
                return cluster_name
    return "Other"


def build_graph_data(repo_filter: str = "", edge_type_filter: str = "", depth: int = 2) -> dict:
    """Extract graph data from DB, optionally filtered."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Get all edges (excluding noisy ones)
    where_clauses = []
    params = []

    noisy_types = NOISY_EDGE_TYPES
    if not edge_type_filter:
        placeholders = ",".join("?" for _ in noisy_types)
        where_clauses.append(f"edge_type NOT IN ({placeholders})")
        params.extend(noisy_types)

    if edge_type_filter:
        where_clauses.append("edge_type = ?")
        params.append(edge_type_filter)

    # Skip unresolved refs and proto detail nodes (too many for viz)
    where_clauses.append("target NOT LIKE 'pkg:%'")
    where_clauses.append("target NOT LIKE 'proto:%'")
    where_clauses.append("target NOT LIKE 'workflow:%'")
    where_clauses.append("target NOT LIKE 'msg:%'")
    where_clauses.append("target NOT LIKE 'svc:%'")
    where_clauses.append("target NOT LIKE 'route:%'")

    where = " AND ".join(where_clauses)

    edges = conn.execute(
        f"SELECT source, target, edge_type, detail FROM graph_edges WHERE {where}",
        params,
    ).fetchall()

    # If repo filter, do BFS to get subgraph
    if repo_filter:
        relevant_nodes = set()
        queue = [repo_filter]
        visited = set()
        for _ in range(depth):
            next_queue = []
            for node in queue:
                if node in visited:
                    continue
                visited.add(node)
                relevant_nodes.add(node)
                for e in edges:
                    if e["source"] == node and e["target"] not in visited:
                        next_queue.append(e["target"])
                    if e["target"] == node and e["source"] not in visited:
                        next_queue.append(e["source"])
            queue = next_queue
        relevant_nodes.update(queue)

        edges = [e for e in edges if e["source"] in relevant_nodes and e["target"] in relevant_nodes]

    # Filter out hub nodes (unless explicitly searching for them)
    if not repo_filter:
        incoming_count = {}
        for e in edges:
            incoming_count[e["target"]] = incoming_count.get(e["target"], 0) + 1
        hub_nodes = {n for n, c in incoming_count.items() if c > HUB_THRESHOLD}
        edges = [e for e in edges if e["target"] not in hub_nodes]

    # Build node set
    all_nodes = set()
    for e in edges:
        all_nodes.add(e["source"])
        all_nodes.add(e["target"])

    # Get node types
    node_types = {}
    if all_nodes:
        placeholders = ",".join("?" for _ in all_nodes)
        rows = conn.execute(
            f"SELECT name, type FROM graph_nodes WHERE name IN ({placeholders})",
            list(all_nodes),
        ).fetchall()
        for r in rows:
            node_types[r["name"]] = r["type"]

    conn.close()

    # Build JSON structure
    nodes = []
    for name in all_nodes:
        ntype = node_types.get(name, "unknown")
        nodes.append(
            {
                "id": name,
                "type": ntype,
                "group": get_cluster(name),
                "highlight": name == repo_filter,
            }
        )

    links = []
    for e in edges:
        links.append(
            {
                "source": e["source"],
                "target": e["target"],
                "type": e["edge_type"],
                "detail": e["detail"] or "",
            }
        )

    return {"nodes": nodes, "links": links}


def generate_html(data: dict, title: str = "Dependency Graph") -> str:
    """Generate self-contained HTML with Sigma.js WebGL visualization.

    Uses graphology-library UMD bundle (includes FA2, noverlap, metrics),
    cluster-aware pre-positioning, multi-phase ForceAtlas2 with LinLog,
    semantic zoom, and muted edges for readability at scale.
    """
    safe_data = json.dumps(data).replace("</", "<\\/")
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    template_path = Path(__file__).parent / "graph_template.html"
    template = template_path.read_text()

    return template.replace("__GRAPH_DATA__", safe_data).replace("__GRAPH_TITLE__", safe_title)


def main():
    repo_filter = ""
    edge_type_filter = ""
    open_browser = False

    for arg in sys.argv[1:]:
        if arg.startswith("--repo="):
            repo_filter = arg.split("=", 1)[1]
        elif arg.startswith("--type="):
            edge_type_filter = arg.split("=", 1)[1]
        elif arg == "--open":
            open_browser = True

    print("Building graph visualization...")
    if repo_filter:
        print(f"  Filtering around: {repo_filter}")
    if edge_type_filter:
        print(f"  Edge type: {edge_type_filter}")

    data = build_graph_data(repo_filter, edge_type_filter)
    print(f"  Nodes: {len(data['nodes'])}, Edges: {len(data['links'])}")

    title = "Dependency Graph"
    if repo_filter:
        title += f" — {repo_filter}"

    html = generate_html(data, title)

    output = OUTPUT_PATH
    if repo_filter:
        output = BASE_DIR / f"graph-{repo_filter}.html"

    output.write_text(html)
    print(f"  Written to: {output}")

    if open_browser:
        subprocess.run(["open", str(output)])
        print("  Opened in browser.")


if __name__ == "__main__":
    main()
