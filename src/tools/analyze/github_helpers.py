"""GitHub API helpers for analyze_task — branches, PRs, task ID matching."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import (
    BATCH_TIMEOUT,
    GH_CACHE_MAX,
    GH_CACHE_TTL,
    MAX_GITHUB_REPOS,
    MAX_WORKERS,
    ORG,
)

_SAFE_REPO_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")

# `gh` CLI is intentionally not installed in this environment (MCP github is used instead).
# Detect once at import time and short-circuit all gh_api() calls when absent — silently,
# so analyze_task degrades to "no GitHub branch/PR enrichment" without log spam.
_GH_AVAILABLE = shutil.which("gh") is not None

# Max repos to query via GitHub API (prevents timeout on large finding sets)
_MAX_GITHUB_REPOS = MAX_GITHUB_REPOS

# Max concurrent GitHub API calls
_MAX_WORKERS = MAX_WORKERS

# Overall timeout for batch GitHub operations (seconds)
_BATCH_TIMEOUT = BATCH_TIMEOUT

# --- GitHub API response cache (thread-safe) ---
_gh_cache: dict[str, tuple[float, dict | list]] = {}  # endpoint → (timestamp, result)
_gh_cache_lock = threading.Lock()
_GH_CACHE_TTL = GH_CACHE_TTL
_GH_CACHE_MAX = GH_CACHE_MAX


def validate_repo_name(repo_name: str) -> bool:
    """Validate repo name contains only safe characters."""
    return bool(_SAFE_REPO_NAME.match(repo_name))


def gh_api(endpoint: str) -> dict | list | None:
    """Call GitHub API via gh CLI. Returns parsed JSON or None on failure.

    Results are cached in-memory with a 10-minute TTL.
    Failures (None) are never cached.
    Returns None immediately if `gh` CLI is not installed.
    """
    if not _GH_AVAILABLE:
        return None

    # Check cache (thread-safe)
    with _gh_cache_lock:
        entry = _gh_cache.get(endpoint)
        if entry is not None:
            ts, cached_result = entry
            if time.time() - ts < _GH_CACHE_TTL:
                _gh_cache[endpoint] = (time.time(), cached_result)  # LRU: update timestamp
                return cached_result
            else:
                del _gh_cache[endpoint]

    # Cache miss — call gh CLI (outside lock to avoid blocking other threads)
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--paginate"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            parsed = json.loads(result.stdout)
            with _gh_cache_lock:
                if len(_gh_cache) >= _GH_CACHE_MAX:
                    oldest_key = min(_gh_cache, key=lambda k: _gh_cache[k][0])
                    del _gh_cache[oldest_key]
                _gh_cache[endpoint] = (time.time(), parsed)
            return parsed
    except Exception as e:
        logging.warning(f"GitHub API error: {e}")
    return None


def clear_gh_cache() -> None:
    """Clear the GitHub API response cache. Used in tests."""
    with _gh_cache_lock:
        _gh_cache.clear()


def task_id_matches(task_id: str, text: str) -> bool:
    """Check if task_id matches in text with word boundary (CORE-100 != CORE-1002)."""
    return bool(re.search(rf"(?i)\b{re.escape(task_id)}\b", text))


def _fetch_branches_for_repo(repo_name: str, task_id: str) -> tuple[str, list[str]]:
    """Fetch matching branches for a single repo. Returns (repo_name, branches)."""
    branches = gh_api(f"repos/{ORG}/{repo_name}/branches")
    if branches and isinstance(branches, list):
        matching = [b["name"] for b in branches if task_id_matches(task_id, b["name"])]
        return repo_name, matching
    return repo_name, []


def _fetch_prs_for_repo(repo_name: str, task_id: str) -> tuple[str, list[dict]]:
    """Fetch matching PRs for a single repo. Returns (repo_name, prs)."""
    prs = gh_api(f"repos/{ORG}/{repo_name}/pulls?state=all&per_page=30")
    if not prs or not isinstance(prs, list):
        return repo_name, []
    matching: list[dict] = []
    for pr in prs:
        head_ref = pr.get("head", {}).get("ref", "")
        title = pr.get("title", "")
        if task_id_matches(task_id, head_ref) or task_id_matches(task_id, title):
            pr_info = {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "merged_at": pr.get("merged_at"),
                "branch": head_ref,
            }
            files = gh_api(f"repos/{ORG}/{repo_name}/pulls/{pr['number']}/files")
            if files and isinstance(files, list):
                pr_info["files"] = [f["filename"] for f in files]
            matching.append(pr_info)
    return repo_name, matching


def find_task_branches(repos: list[str], task_id: str) -> dict[str, list[str]]:
    """Search for branches matching task_id in given repos (parallel)."""
    valid_repos = [r for r in repos[:_MAX_GITHUB_REPOS] if validate_repo_name(r)]
    if not valid_repos:
        return {}

    results: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_branches_for_repo, repo, task_id): repo for repo in valid_repos}
        for future in as_completed(futures, timeout=_BATCH_TIMEOUT):
            try:
                repo_name, branches = future.result(timeout=1)
                if branches:
                    results[repo_name] = branches
            except Exception as e:
                repo = futures[future]
                print(f"[github_helpers] failed to fetch branches for {repo}: {e}", file=sys.stderr)
                continue
    return results


def find_task_prs(repos: list[str], task_id: str) -> dict[str, list[dict]]:
    """Search for PRs matching task_id in given repos (parallel)."""
    valid_repos = [r for r in repos[:_MAX_GITHUB_REPOS] if validate_repo_name(r)]
    if not valid_repos:
        return {}

    results: dict[str, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_prs_for_repo, repo, task_id): repo for repo in valid_repos}
        for future in as_completed(futures, timeout=_BATCH_TIMEOUT):
            try:
                repo_name, prs = future.result(timeout=1)
                if prs:
                    results[repo_name] = prs
            except Exception as e:
                repo = futures[future]
                print(f"[github_helpers] failed to fetch PRs for {repo}: {e}", file=sys.stderr)
                continue
    return results
