"""GitHub API helpers for analyze_task — branches, PRs, task ID matching."""

from __future__ import annotations

import json
import logging
import re
import subprocess

from src.config import ORG

_SAFE_REPO_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


def validate_repo_name(repo_name: str) -> bool:
    """Validate repo name contains only safe characters."""
    return bool(_SAFE_REPO_NAME.match(repo_name))


def gh_api(endpoint: str) -> dict | list | None:
    """Call GitHub API via gh CLI. Returns parsed JSON or None on failure."""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint, "--paginate"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except Exception as e:
        logging.warning(f"GitHub API error: {e}")
    return None


def task_id_matches(task_id: str, text: str) -> bool:
    """Check if task_id matches in text with word boundary (CORE-100 != CORE-1002)."""
    return bool(re.search(rf"(?i)\b{re.escape(task_id)}\b", text))


def find_task_branches(repos: list[str], task_id: str) -> dict[str, list[str]]:
    """Search for branches matching task_id in given repos."""
    results: dict[str, list[str]] = {}
    for repo_name in repos:
        if not validate_repo_name(repo_name):
            continue
        branches = gh_api(f"repos/{ORG}/{repo_name}/branches")
        if branches and isinstance(branches, list):
            matching = [b["name"] for b in branches if task_id_matches(task_id, b["name"])]
            if matching:
                results[repo_name] = matching
    return results


def find_task_prs(repos: list[str], task_id: str) -> dict[str, list[dict]]:
    """Search for PRs matching task_id in given repos."""
    results: dict[str, list[dict]] = {}
    for repo_name in repos:
        if not validate_repo_name(repo_name):
            continue
        prs = gh_api(f"repos/{ORG}/{repo_name}/pulls?state=all&per_page=30")
        if not prs or not isinstance(prs, list):
            continue
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
        if matching:
            results[repo_name] = matching
    return results
