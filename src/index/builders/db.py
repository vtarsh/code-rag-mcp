"""SQLite schema creation and per-repo deletion utilities."""

from __future__ import annotations

import sqlite3
import sys

from ._common import _BASE_DIR


def create_db(conn: sqlite3.Connection):
    """Create FTS5 tables and metadata tables."""
    conn.executescript("""
        -- Main FTS5 search table
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
            content,
            repo_name,
            file_path,
            file_type,
            chunk_type,
            language,
            tokenize='porter unicode61'
        );

        -- Repo metadata (non-FTS)
        CREATE TABLE IF NOT EXISTS repos (
            name TEXT PRIMARY KEY,
            type TEXT,
            sha TEXT,
            org_deps TEXT,  -- JSON array
            artifact_counts TEXT  -- JSON object
        );

        -- Chunk ordering metadata (for sibling retrieval in hybrid search)
        CREATE TABLE IF NOT EXISTS chunk_meta (
            chunk_rowid INTEGER PRIMARY KEY,  -- references chunks rowid
            chunk_order INTEGER NOT NULL,      -- 0-based order within (repo, file)
            total_chunks INTEGER NOT NULL      -- total chunks in this (repo, file)
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_meta_order
            ON chunk_meta(chunk_rowid);

        -- Build metadata
        CREATE TABLE IF NOT EXISTS build_info (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- Code facts: validation guards, const values, joi/zod schemas
        CREATE TABLE IF NOT EXISTS code_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            function_name TEXT,
            fact_type TEXT NOT NULL,  -- 'validation_guard', 'const_value', 'joi_schema', 'require_chain'
            condition TEXT,           -- the if-condition or const name
            message TEXT,             -- error message or const value
            line_number INTEGER,
            raw_snippet TEXT           -- 3-5 lines of context
        );
        CREATE INDEX IF NOT EXISTS idx_code_facts_repo ON code_facts(repo_name);
        CREATE INDEX IF NOT EXISTS idx_code_facts_type ON code_facts(fact_type);

        -- FTS5 for code_facts (searchable)
        CREATE VIRTUAL TABLE IF NOT EXISTS code_facts_fts USING fts5(
            repo_name,
            file_path,
            function_name,
            fact_type,
            condition,
            message,
            content=code_facts,
            content_rowid=id,
            tokenize='porter unicode61'
        );
    """)


def delete_repo_chunks(conn: sqlite3.Connection, repo_name: str) -> int:
    """Delete all FTS5 chunks for a specific repo. Returns count of deleted rows."""
    # FTS5 supports DELETE with rowid. We need to find rowids first.
    rowids = conn.execute("SELECT rowid FROM chunks WHERE repo_name = ?", (repo_name,)).fetchall()
    for (rowid,) in rowids:
        conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))
    return len(rowids)


def delete_repo_data(conn: sqlite3.Connection, repo_name: str) -> int:
    """Delete all SQLite data for a repo: chunks, chunk_meta, code_facts, code_facts_fts, repos row.

    SQLite-only. For full cross-layer cleanup (including LanceDB vectors),
    use ``reset_repo_all_layers()``.

    Returns count of deleted chunk rows.
    """
    # Delete chunks and track rowids for chunk_meta cleanup
    rowids = conn.execute("SELECT rowid FROM chunks WHERE repo_name = ?", (repo_name,)).fetchall()
    for (rowid,) in rowids:
        conn.execute("DELETE FROM chunks WHERE rowid = ?", (rowid,))

    # Delete chunk_meta for those rowids
    if rowids:
        rowid_list = [r[0] for r in rowids]
        placeholders = ",".join("?" * len(rowid_list))
        conn.execute(f"DELETE FROM chunk_meta WHERE chunk_rowid IN ({placeholders})", rowid_list)

    # Delete code_facts_fts entries (content table sync — must delete before code_facts)
    fact_ids = conn.execute("SELECT id FROM code_facts WHERE repo_name = ?", (repo_name,)).fetchall()
    for (fid,) in fact_ids:
        conn.execute("DELETE FROM code_facts_fts WHERE rowid = ?", (fid,))

    # Delete code_facts
    conn.execute("DELETE FROM code_facts WHERE repo_name = ?", (repo_name,))

    # Delete repos row
    conn.execute("DELETE FROM repos WHERE name = ?", (repo_name,))

    return len(rowids)


def _delete_lancedb_repo(lance_dir_name: str, repo_name: str) -> int:
    """Delete all vectors for a repo from one LanceDB store.

    Returns count of deleted rows, or 0 if the store doesn't exist / is empty.
    """
    lance_path = _BASE_DIR / "db" / lance_dir_name
    if not lance_path.exists():
        return 0
    try:
        import lancedb
    except ImportError:
        print(f"  WARNING: lancedb not installed, skipping {lance_dir_name}", file=sys.stderr)
        return 0
    try:
        db = lancedb.connect(str(lance_path))
        if "chunks" not in db.table_names():
            return 0
        table = db.open_table("chunks")
        before = table.count_rows()
        safe = repo_name.replace("'", "''")
        table.delete(f"repo_name = '{safe}'")
        return before - table.count_rows()
    except Exception as e:
        print(f"  WARNING: could not clean {lance_dir_name} for {repo_name}: {e}", file=sys.stderr)
        return 0


def reset_repo_all_layers(conn: sqlite3.Connection, repo_name: str) -> dict[str, int]:
    """Fully reset all data for a single repo across ALL storage layers.

    Cleans:
      1-5. SQLite: chunks, chunk_meta, code_facts, code_facts_fts, repos
      6.   LanceDB vectors.lance.coderank (if exists)

    NOT cleaned here (by design):
      - graph_edges / graph_nodes — graph is rebuilt as a whole, never per-repo
        (see scripts/build_graph.py which always DROPs and rebuilds)
      - raw/, extracted/ — filesystem artifacts, untouched by SQLite cleanup

    Use this whenever a repo needs to be removed or fully re-indexed to
    prevent orphan vectors (which `delete_repo_data` alone leaves behind).

    CAUTION: ``repo_name`` can be a pseudo-repo used for scraped docs
    (e.g. ``silverflow-docs``, ``volt-docs``, ``provider-worldpay-*``,
    ``apm-redirect-flow``). These are legitimate rows in SQLite ``chunks``
    and LanceDB — never bulk-delete them based on ``repos`` table absence.
    Pass only a known, verified repo name.

    Returns dict with per-layer deletion counts.
    """
    stats: dict[str, int] = {}
    stats["sqlite_chunks"] = delete_repo_data(conn, repo_name)
    stats["lance_coderank"] = _delete_lancedb_repo("vectors.lance.coderank", repo_name)
    return stats
