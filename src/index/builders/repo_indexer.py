"""Per-repo indexer — walks extracted artifacts and inserts chunks + code_facts."""

from __future__ import annotations

import json
import sqlite3

from ._common import EXTRACTED_DIR
from .code_facts import extract_code_facts
from .detect import detect_file_type, detect_language
from .dispatcher import chunk_file


def index_repo(conn: sqlite3.Connection, repo_name: str, meta: dict) -> tuple[int, int]:
    """Index a single repo. Returns (files_count, chunks_count)."""
    artifact_types = [
        "proto",
        "docs",
        "config",
        "env",
        "k8s",
        "methods",
        "libs",
        "workflows",
        "ci",
        "routes",
        "services",
        "handlers",
        "utils",
        "consts",
        # Frontend source: extract_artifacts.py writes raw src/ files to
        # extracted/{repo}/src/ (and packages/*/src, apps/*/src) so that
        # rel_path matches Jira files_changed entries (e.g. src/Pages/X.tsx).
        "src",
        "packages",
        "apps",
    ]

    repo_dir = EXTRACTED_DIR / repo_name
    if not repo_dir.is_dir():
        # Still insert repo metadata so incremental mode can track its SHA
        conn.execute(
            "INSERT OR REPLACE INTO repos(name, type, sha, org_deps, artifact_counts) VALUES (?, ?, ?, ?, ?)",
            (
                repo_name,
                meta.get("type", "unknown"),
                meta.get("sha", "unknown"),
                json.dumps(meta.get("org_deps", [])),
                json.dumps(meta.get("artifacts", {})),
            ),
        )
        return 0, 0

    files = 0
    chunks = 0

    for artifact_type in artifact_types:
        type_dir = repo_dir / artifact_type
        if not type_dir.is_dir():
            continue

        for file_path in type_dir.rglob("*"):
            if not file_path.is_file():
                continue

            files += 1
            rel_path = str(file_path.relative_to(repo_dir))
            language = detect_language(str(file_path))
            file_type = detect_file_type(artifact_type, file_path.name)

            file_chunks = chunk_file(file_path, repo_name, artifact_type)
            chunk_rowids = []

            for chunk in file_chunks:
                conn.execute(
                    "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        chunk["content"],
                        repo_name,
                        rel_path,
                        file_type,
                        chunk["chunk_type"],
                        language,
                    ),
                )
                chunk_rowids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                chunks += 1

            # Populate chunk_meta for sibling retrieval
            total = len(chunk_rowids)
            for order, rowid in enumerate(chunk_rowids):
                conn.execute(
                    "INSERT OR REPLACE INTO chunk_meta(chunk_rowid, chunk_order, total_chunks) VALUES (?, ?, ?)",
                    (rowid, order, total),
                )

            # Extract code_facts from JS files in relevant directories
            if language in ("javascript", "typescript") and artifact_type in (
                "methods",
                "libs",
                "handlers",
                "routes",
                "services",
                "utils",
                "consts",
            ):
                try:
                    source = file_path.read_text(encoding="utf-8", errors="replace")
                    facts = extract_code_facts(source, rel_path, repo_name)
                    for fact in facts:
                        conn.execute(
                            "INSERT INTO code_facts(repo_name, file_path, function_name, fact_type, "
                            "condition, message, line_number, raw_snippet) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                fact["repo_name"],
                                fact["file_path"],
                                fact["function_name"],
                                fact["fact_type"],
                                fact["condition"],
                                fact["message"],
                                fact["line_number"],
                                fact["raw_snippet"],
                            ),
                        )
                        # Also insert into FTS5 for searchability
                        conn.execute(
                            "INSERT INTO code_facts_fts(rowid, repo_name, file_path, function_name, "
                            "fact_type, condition, message) "
                            "VALUES (last_insert_rowid(), ?, ?, ?, ?, ?, ?)",
                            (
                                fact["repo_name"],
                                fact["file_path"],
                                fact["function_name"],
                                fact["fact_type"],
                                fact["condition"],
                                fact["message"],
                            ),
                        )
                except Exception:
                    pass  # Don't fail indexing on code_facts extraction errors

    # Insert/update repo metadata
    conn.execute(
        "INSERT OR REPLACE INTO repos(name, type, sha, org_deps, artifact_counts) VALUES (?, ?, ?, ?, ?)",
        (
            repo_name,
            meta.get("type", "unknown"),
            meta.get("sha", "unknown"),
            json.dumps(meta.get("org_deps", [])),
            json.dumps(meta.get("artifacts", {})),
        ),
    )

    return files, chunks
