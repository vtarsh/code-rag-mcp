"""Manual edges declared via conventions.yaml manual_edges.{group}."""

import sqlite3

from ._common import MANUAL_EDGES, _resolve_repo_ref


def parse_manual_edges(conn: sqlite3.Connection, group: str):
    """Create graph edges from conventions.yaml manual_edges entries.

    Each group (e.g. 'connection_validation', 'merchant_entity') is a list of
    {source, target, edge_type, detail} dicts. Repo name placeholders like
    {gateway_repo} and {feature_repo} are resolved from conventions config.
    """
    edge_defs = MANUAL_EDGES.get(group, [])
    if not edge_defs:
        print(f"  {group} edges: 0 (no config)")
        return

    known_repos = {r[0] for r in conn.execute("SELECT name FROM graph_nodes").fetchall()}
    edges: list[tuple[str, str, str, str]] = []

    for entry in edge_defs:
        source = _resolve_repo_ref(entry["source"])
        target = _resolve_repo_ref(entry["target"])
        if source in known_repos and target in known_repos:
            edges.append((source, target, entry["edge_type"], entry.get("detail", "")))

    if edges:
        conn.executemany(
            "INSERT OR IGNORE INTO graph_edges (source, target, edge_type, detail) VALUES (?, ?, ?, ?)",
            edges,
        )

    print(f"  {group} edges: {len(edges)}")
