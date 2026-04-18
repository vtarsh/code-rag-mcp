"""K8s deployment config edges."""

import re
import sqlite3


def parse_k8s_env_edges(conn: sqlite3.Connection):
    """Parse K8s deployment configs for service references."""
    edges = []

    rows = conn.execute(
        "SELECT repo_name, content FROM chunks WHERE file_type = 'k8s' AND content LIKE '%GRPC%'"
    ).fetchall()

    repo_names = set(r[0] for r in conn.execute("SELECT name FROM repos").fetchall())

    grpc_ref_pattern = re.compile(r'value:\s*[\'"]?([a-z][\w-]+)\.grpc\.', re.IGNORECASE)

    for row in rows:
        source = row[0]
        content = row[1]

        for match in grpc_ref_pattern.finditer(content):
            hostname = match.group(1)
            target_candidates = [f"grpc-{hostname}", hostname]

            for candidate in target_candidates:
                if candidate in repo_names and candidate != source:
                    edges.append((source, candidate, "k8s_env", hostname))
                    break

    unique_edges = list(set(edges))
    for e in unique_edges:
        conn.execute("INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)", e)
    print(f"  K8s env edges: {len(unique_edges)}")
