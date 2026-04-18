"""Indexers that read directly from raw/ — seeds.cql and per-repo test scripts."""

from __future__ import annotations

import sqlite3

from ._common import FEATURE_REPO, MAX_CHUNK, MIN_CHUNK, RAW_DIR
from .cql_chunks import chunk_cql_seeds
from .detect import detect_language


def index_seeds(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index seeds.cql from feature repo as provider config source of truth.

    Each INSERT = separate chunk with provider, payment_method_type, features, currencies.
    """
    seeds_path = RAW_DIR / FEATURE_REPO / "seeds.cql"
    if not seeds_path.is_file():
        return 0, 0

    try:
        content = seeds_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0, 0

    chunks = chunk_cql_seeds(content, FEATURE_REPO)
    count = 0

    for chunk in chunks:
        conn.execute(
            "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk["content"],
                FEATURE_REPO,
                "seeds.cql",
                "provider_config",
                chunk["chunk_type"],
                "cql",
            ),
        )
        count += 1

    if count:
        print(f"  Seeds.cql: {count} provider config chunks")

    return 1 if count else 0, count


def index_test_scripts(conn: sqlite3.Connection) -> tuple[int, int]:
    """Index scripts/ directories from provider repos.

    Test scripts contain credentials, correct request formats, URLs — valuable for
    onboarding and debugging provider integrations.
    """
    if not RAW_DIR.is_dir():
        return 0, 0

    files = 0
    chunks = 0

    # Index scripts from all repos that have a scripts/ directory
    for repo_dir in sorted(RAW_DIR.iterdir()):
        if not repo_dir.is_dir():
            continue
        scripts_dir = repo_dir / "scripts"
        if not scripts_dir.is_dir():
            continue

        repo_name = repo_dir.name

        for script_path in sorted(scripts_dir.rglob("*")):
            if not script_path.is_file():
                continue
            ext = script_path.suffix.lower()
            if ext not in (".js", ".ts", ".sh", ".mjs", ".py"):
                continue

            try:
                content = script_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            if not content.strip() or len(content.strip()) < MIN_CHUNK:
                continue

            files += 1
            language = detect_language(str(script_path))
            rel_path = f"scripts/{script_path.relative_to(scripts_dir)}"

            # Mask potential secrets but keep structure
            text = content.strip()
            if len(text) > MAX_CHUNK:
                text = text[:MAX_CHUNK] + "\n... [truncated]"

            conn.execute(
                "INSERT INTO chunks(content, repo_name, file_path, file_type, chunk_type, language) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"[Repo: {repo_name}] [Test Script] {text}",
                    repo_name,
                    rel_path,
                    "test_script",
                    "code_file",
                    language,
                ),
            )
            chunks += 1

    if files:
        print(f"  Test scripts: {files} files, {chunks} chunks")

    return files, chunks
