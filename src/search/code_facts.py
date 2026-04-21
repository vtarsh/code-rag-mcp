"""FTS5 search over code_facts — structured facts (schemas, env lookups, guards, retry policies).

Complements chunks_fts by matching on fact_type/condition/message/function_name
rather than raw chunk content. Each hit maps to an existing chunk via
(repo_name, file_path) so the hybrid pipeline can fuse with chunks-based
candidates via RRF.

Built by `src/index/builders/code_facts.py`; 1659 rows over 1035 (repo, file)
pairs at time of wiring. Was unread at query time before P0c.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from src.container import db_connection
from src.search.fts import sanitize_fts_query


def code_facts_search(
    query: str,
    repo: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search code_facts_fts, return (repo_name, file_path, fact_type, condition, message, line_number).

    Each returned row identifies a specific code fact (e.g. joi_schema,
    validation_guard, env_var, const_value, temporal_retry, grpc_status) that
    matched the query. The caller maps these to chunks via (repo_name, file_path).

    Args:
        query: Raw user query (will be sanitized for FTS5 via sanitize_fts_query).
        repo: Optional repo substring filter.
        limit: Maximum number of rows to fetch.

    Returns ordered list of dicts by FTS5 rank; empty list on no match or error.
    """
    fts_query = sanitize_fts_query(query)
    if not fts_query:
        return []

    where = ["code_facts_fts MATCH ?"]
    params: list[Any] = [fts_query]
    if repo:
        where.append("cf.repo_name LIKE ?")
        params.append(f"%{repo}%")
    params.append(limit)

    with db_connection() as conn:
        try:
            rows = conn.execute(
                f"""SELECT cf.repo_name, cf.file_path, cf.fact_type, cf.condition,
                          cf.message, cf.line_number
                   FROM code_facts_fts ff
                   JOIN code_facts cf ON cf.rowid = ff.rowid
                   WHERE {" AND ".join(where)}
                   ORDER BY ff.rank LIMIT ?""",
                params,
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []


def fetch_chunks_for_files(pairs: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Fetch the first chunk for each (repo_name, file_path) pair.

    Used to inject candidates that code_facts matched on but chunks_fts missed.
    We return a single chunk per file (the lowest-rowid one, typically the
    file header / first code block) to avoid flooding the RRF pool.

    Returns list of dicts with: rowid, repo_name, file_path, file_type,
    chunk_type, snippet.
    """
    if not pairs:
        return []

    results: list[dict[str, Any]] = []
    with db_connection() as conn:
        for repo_name, file_path in pairs:
            row = conn.execute(
                """SELECT rowid, repo_name, file_path, file_type, chunk_type,
                          substr(content, 1, 400) AS snippet
                   FROM chunks
                   WHERE repo_name = ? AND file_path = ?
                   ORDER BY rowid LIMIT 1""",
                (repo_name, file_path),
            ).fetchone()
            if row:
                results.append(dict(row))
    return results
