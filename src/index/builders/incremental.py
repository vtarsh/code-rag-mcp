"""Incremental-build helpers: SHA detection across repos + profile-doc fingerprint."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path

from ._common import (
    DICTIONARY_DIR,
    DOMAIN_REGISTRY_FILE,
    FEATURE_REPO,
    FLOWS_DIR,
    GOTCHAS_DIR,
    PROVIDERS_DIR,
    RAW_DIR,
    REFERENCES_DIR,
    TASKS_DIR,
)


def load_existing_shas(conn: sqlite3.Connection) -> dict[str, str]:
    """Load repo name -> SHA mapping from existing database."""
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT name, sha FROM repos")}
    except sqlite3.OperationalError:
        return {}  # table doesn't exist yet


def get_current_sha(repo_path: Path) -> str | None:
    """Get current HEAD SHA from a git repo directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def detect_changed_repos(repo_meta: dict, existing_shas: dict[str, str]) -> tuple[set[str], set[str]]:
    """Compare current HEAD SHAs with indexed SHAs.

    Returns (changed_repos, removed_repos).
    changed_repos: repos that need re-indexing (new or SHA mismatch).
    removed_repos: repos in DB but no longer in raw/.
    """
    changed = set()
    current_repos = set()

    for repo_name, meta in repo_meta.items():
        current_repos.add(repo_name)
        indexed_sha = existing_shas.get(repo_name)

        # New repo or SHA changed
        raw_path = RAW_DIR / repo_name
        if raw_path.is_dir():
            current_sha = get_current_sha(raw_path)
            # Fall back to _index.json SHA if git not available
            if current_sha is None:
                current_sha = meta.get("sha", "unknown")
        else:
            current_sha = meta.get("sha", "unknown")

        if indexed_sha is None or indexed_sha != current_sha:
            changed.add(repo_name)

    # Repos in DB but no longer in index
    removed = set(existing_shas.keys()) - current_repos

    return changed, removed


def compute_profile_docs_fingerprint() -> str:
    """Hash mtimes of all profile-doc sources + seeds.cql.

    Used to gate re-indexing of provider/gotchas/references/dictionary chunks
    so that quiet incremental runs don't churn ~22 k SQLite rowids and trigger
    a downstream LanceDB re-embed storm.
    """
    h = hashlib.md5()
    sources = [
        GOTCHAS_DIR,
        FLOWS_DIR,
        TASKS_DIR,
        REFERENCES_DIR,
        DICTIONARY_DIR,
        PROVIDERS_DIR,
        DOMAIN_REGISTRY_FILE,
    ]
    for src in sources:
        if src is None:
            continue
        if src.is_file():
            h.update(f"{src}:{src.stat().st_mtime_ns}\n".encode())
        elif src.is_dir():
            for f in sorted(src.rglob("*")):
                if f.is_file():
                    h.update(f"{f.relative_to(src)}:{f.stat().st_mtime_ns}\n".encode())
    # seeds.cql + test scripts depend on raw/, but they're small (<200
    # chunks combined). Include seeds file in fingerprint; skip
    # test scripts (gated by repo SHA comparison upstream).
    seeds_path = RAW_DIR / FEATURE_REPO / "seeds.cql"
    if seeds_path.is_file():
        h.update(f"{seeds_path}:{seeds_path.stat().st_mtime_ns}\n".encode())
    return h.hexdigest()
