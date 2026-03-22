"""Method existence checking for analyze_task."""

from __future__ import annotations

import sqlite3


def check_method_exists(repo_name: str, method_name: str, conn: sqlite3.Connection) -> dict:
    """Check if a gRPC method already exists in a repo."""
    chunks = conn.execute(
        "SELECT file_path, content FROM chunks WHERE repo_name = ? AND file_type = 'grpc_method' AND file_path LIKE ?",
        (repo_name, f"%{method_name}%"),
    ).fetchall()

    if chunks:
        return {"exists": True, "file_path": chunks[0]["file_path"], "snippet": chunks[0]["content"][:200]}

    registry = conn.execute(
        "SELECT content FROM chunks WHERE repo_name = ? AND file_path LIKE '%methods/index%'", (repo_name,)
    ).fetchall()
    if registry:
        for r in registry:
            if method_name.lower() in r["content"].lower():
                return {
                    "exists": True,
                    "file_path": "methods/index.js",
                    "snippet": f"'{method_name}' registered in method index",
                }

    return {"exists": False}
