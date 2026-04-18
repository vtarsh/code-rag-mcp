"""Database schema, node population, and summary printing."""

import sqlite3


def init_graph_tables(conn: sqlite3.Connection):
    """Create graph tables if they don't exist."""
    conn.executescript("""
        DROP TABLE IF EXISTS graph_edges;
        DROP TABLE IF EXISTS graph_nodes;

        CREATE TABLE graph_nodes (
            name TEXT PRIMARY KEY,
            type TEXT,           -- grpc-service-js, temporal-workflow, library, etc.
            grpc_host TEXT,      -- e.g. core-merchants.grpc.example.com
            proto_package TEXT   -- e.g. provider, core.merchants
        );

        CREATE TABLE graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,       -- repo that depends/calls
            target TEXT NOT NULL,       -- repo being depended on/called
            edge_type TEXT NOT NULL,    -- grpc_call, npm_dep, proto_import, k8s_env
            detail TEXT,               -- extra info (env var name, package name, etc.)
            UNIQUE(source, target, edge_type, detail)
        );

        CREATE INDEX idx_edges_source ON graph_edges(source);
        CREATE INDEX idx_edges_target ON graph_edges(target);
        CREATE INDEX idx_edges_type ON graph_edges(edge_type);
        CREATE INDEX IF NOT EXISTS idx_edges_source_type ON graph_edges(source, edge_type);
        CREATE INDEX IF NOT EXISTS idx_edges_target_type ON graph_edges(target, edge_type);
    """)
    conn.commit()


def populate_nodes(conn: sqlite3.Connection):
    """Create a node for every repo."""
    repos = conn.execute("SELECT name, type FROM repos").fetchall()
    for r in repos:
        conn.execute("INSERT OR IGNORE INTO graph_nodes (name, type) VALUES (?, ?)", (r[0], r[1]))
    print(f"  Nodes: {len(repos)}")


def print_summary(conn: sqlite3.Connection):
    """Print graph statistics."""
    nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]

    print("\n=== Graph Summary ===")
    print(f"  Nodes: {nodes}")
    print(f"  Edges: {edges}")

    print("\n  Edge types:")
    for row in conn.execute("SELECT edge_type, COUNT(*) as cnt FROM graph_edges GROUP BY edge_type ORDER BY cnt DESC"):
        print(f"    {row[0]}: {row[1]}")

    # Top connected services
    print("\n  Most depended-on (top 15):")
    for row in conn.execute("""
        SELECT target, COUNT(*) as cnt
        FROM graph_edges
        WHERE target NOT LIKE 'pkg:%' AND target NOT LIKE 'proto:%'
        GROUP BY target
        ORDER BY cnt DESC
        LIMIT 15
    """):
        print(f"    {row[0]}: {row[1]} incoming edges")

    print("\n  Most dependencies (top 10):")
    for row in conn.execute("""
        SELECT source, COUNT(*) as cnt
        FROM graph_edges
        GROUP BY source
        ORDER BY cnt DESC
        LIMIT 10
    """):
        print(f"    {row[0]}: {row[1]} outgoing edges")
