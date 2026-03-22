#!/usr/bin/env python3
"""
Phase 8: Collect Jira task + GitHub PRs into task_history.

Usage:
    python scripts/collect_task.py PI-54
    python scripts/collect_task.py PI-54 --dry-run   # print without saving
    python scripts/collect_task.py PI-54 --force      # overwrite existing
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from base64 import b64encode
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"

JIRA_DOMAIN = "pay-com.atlassian.net"
JIRA_EMAIL = os.getenv("JIRA_EMAIL", "vyacheslav.t@pay.com")
JIRA_TOKEN = os.getenv("JIRA_API_TOKEN", "")
GH_ORG = "pay-com"

# Fallback: read JIRA_API_TOKEN from ~/.zshrc if not in env
if not JIRA_TOKEN:
    _zshrc = Path.home() / ".zshrc"
    if _zshrc.exists():
        import re as _re

        for line in _zshrc.read_text().splitlines():
            m = _re.match(r"""export\s+JIRA_API_TOKEN=["']?([^"'\s]+)["']?""", line)
            if m:
                JIRA_TOKEN = m.group(1)
                break


# ---------------------------------------------------------------------------
# Jira API helpers
# ---------------------------------------------------------------------------


def _jira_headers() -> dict[str, str]:
    cred = b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {cred}", "Accept": "application/json"}


def _jira_get(path: str) -> dict:
    url = f"https://{JIRA_DOMAIN}/rest/api/3/{path}"
    req = urllib.request.Request(url, headers=_jira_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_jira(ticket_id: str) -> dict:
    """Fetch full ticket data from Jira REST API."""
    fields = (
        "summary,description,issuetype,status,assignee,comment,"
        "parent,subtasks,issuelinks,labels,components,attachment,"
        "customfield_10016,customfield_10014,customfield_10020"
        # 10016=story points, 10014=epic link, 10020=sprint
    )
    data = _jira_get(f"issue/{ticket_id}?fields={fields}&expand=changelog")
    f = data["fields"]

    # Description: ADF → plain text (simplified)
    desc_raw = f.get("description")
    description = _adf_to_text(desc_raw) if desc_raw else ""

    # Comments
    comments = []
    for c in f.get("comment", {}).get("comments") or []:
        comments.append(
            {
                "author": (c.get("author", {}).get("displayName") or ""),
                "body": _adf_to_text(c.get("body", {})),
                "date": c.get("created", ""),
            }
        )

    # Subtasks
    subtasks = [s["key"] for s in (f.get("subtasks") or [])]

    # Linked issues
    linked: dict[str, list[str]] = {"blocks": [], "caused_by": [], "relates_to": []}
    for link in f.get("issuelinks") or []:
        lt = link.get("type", {}).get("name", "").lower()
        target = link.get("outwardIssue") or link.get("inwardIssue")
        if not target:
            continue
        key = target["key"]
        if "block" in lt:
            linked["blocks"].append(key)
        elif "cause" in lt:
            linked["caused_by"].append(key)
        else:
            linked["relates_to"].append(key)

    # Labels, components
    labels = f.get("labels") or []
    components = [c["name"] for c in (f.get("components") or [])]

    # Sprint (customfield_10020 is common for Jira Cloud)
    sprint_field = f.get("customfield_10020")
    sprint = ""
    if isinstance(sprint_field, list) and sprint_field:
        last = sprint_field[-1]
        sprint = last.get("name", "") if isinstance(last, dict) else str(last)
    elif isinstance(sprint_field, dict):
        sprint = sprint_field.get("name", "")

    # Story points (customfield_10016 is common)
    story_points = f.get("customfield_10016")

    # Epic
    epic_id = ""
    if f.get("parent", {}).get("fields", {}).get("issuetype", {}).get("name") == "Epic":
        epic_id = f["parent"]["key"]
    elif f.get("customfield_10014"):
        epic_id = str(f["customfield_10014"])

    parent_id = f.get("parent", {}).get("key", "") if f.get("parent") else ""
    if parent_id == epic_id:
        parent_id = ""

    # Status changelog
    changelog = []
    for history in data.get("changelog", {}).get("histories") or []:
        for item in history.get("items", []):
            if item.get("field") == "status":
                changelog.append(
                    {
                        "from": item.get("fromString", ""),
                        "to": item.get("toString", ""),
                        "date": history.get("created", ""),
                    }
                )

    # Attachments (URLs only for MVP)
    attachments = []
    for a in f.get("attachment") or []:
        attachments.append(
            {
                "name": a.get("filename", ""),
                "url": a.get("content", ""),
                "type": a.get("mimeType", ""),
            }
        )

    return {
        "ticket_id": ticket_id,
        "ticket_type": f.get("issuetype", {}).get("name", ""),
        "summary": f.get("summary", ""),
        "description": description,
        "developer": (f.get("assignee") or {}).get("displayName", ""),
        "epic_id": epic_id,
        "parent_id": parent_id,
        "subtasks": subtasks,
        "linked_issues": linked,
        "labels": labels,
        "components": components,
        "sprint": sprint,
        "story_points": story_points,
        "jira_status": f.get("status", {}).get("name", ""),
        "status_changelog": changelog,
        "jira_comments": comments,
        "attachments": attachments,
    }


def _adf_to_text(node: dict | list | str | None) -> str:
    """Convert Atlassian Document Format to plain text (best-effort)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(n) for n in node)
    if isinstance(node, dict):
        text = node.get("text", "")
        children = node.get("content", [])
        child_text = "".join(_adf_to_text(c) for c in children) if children else ""
        ntype = node.get("type", "")
        if ntype in ("paragraph", "heading", "bulletList", "orderedList", "listItem"):
            return child_text + "\n"
        if ntype == "hardBreak":
            return "\n"
        if ntype == "codeBlock":
            return child_text + "\n"
        return text + child_text
    return ""


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------


def _gh(*args: str, json_output: bool = True) -> list | dict | str:
    """Run gh CLI command and return parsed JSON or raw text."""
    cmd = ["/usr/local/bin/gh", *args]
    if json_output and "--json" not in args:
        pass  # caller provides --json
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        # Some gh search commands return 1 with empty results
        if "no results" in result.stderr.lower() or not result.stdout.strip():
            return [] if json_output else ""
        print(f"  [warn] gh {' '.join(args[:3])}: {result.stderr.strip()}", file=sys.stderr)
        return [] if json_output else ""
    if not result.stdout.strip():
        return [] if json_output else ""
    if json_output:
        return json.loads(result.stdout)
    return result.stdout.strip()


def fetch_github_prs(ticket_id: str) -> dict:
    """Find all PRs and commits for a ticket across pay-com org."""
    # Step 1: Search PRs by ticket ID in title/body
    prs_raw = _gh(
        "search",
        "prs",
        ticket_id,
        "--owner",
        GH_ORG,
        "--json",
        "url,title,number,repository",
        "--limit",
        "50",
    )
    if not isinstance(prs_raw, list):
        prs_raw = []

    # Step 2: Search commits (may find changes without PR)
    commits_raw = _gh(
        "search",
        "commits",
        ticket_id,
        "--owner",
        GH_ORG,
        "--json",
        "repository,sha,commit",
        "--limit",
        "50",
    )
    if not isinstance(commits_raw, list):
        commits_raw = []

    # Deduplicate repos from commits
    commit_repos = set()
    for c in commits_raw:
        repo_info = c.get("repository", {})
        name = repo_info.get("name", "") if isinstance(repo_info, dict) else ""
        if name:
            commit_repos.add(name)

    # Build unique PR set
    pr_map: dict[str, dict] = {}  # url -> pr info
    for pr in prs_raw:
        repo = pr.get("repository", {})
        repo_name = repo.get("name", "") if isinstance(repo, dict) else ""
        pr_map[pr["url"]] = {
            "url": pr["url"],
            "title": pr.get("title", ""),
            "number": pr.get("number"),
            "repo": repo_name,
        }

    # Step 3: For repos found in commits but not in PRs, search directly
    pr_repos = {p["repo"] for p in pr_map.values()}
    for repo_name in commit_repos - pr_repos:
        extra = _gh(
            "pr",
            "list",
            "--repo",
            f"{GH_ORG}/{repo_name}",
            "--search",
            ticket_id,
            "--state",
            "all",
            "--json",
            "url,title,number",
            "--limit",
            "20",
        )
        if isinstance(extra, list):
            for pr in extra:
                if pr.get("url") and pr["url"] not in pr_map:
                    pr_map[pr["url"]] = {
                        "url": pr["url"],
                        "title": pr.get("title", ""),
                        "number": pr.get("number"),
                        "repo": repo_name,
                    }

    # Step 4: For each PR, get files and review comments
    all_files: list[str] = []
    all_repos: set[str] = set()
    all_review_comments: list[dict] = []
    pr_urls: list[str] = []

    for pr_info in pr_map.values():
        repo_name = pr_info["repo"]
        number = pr_info["number"]
        if not repo_name or not number:
            continue

        all_repos.add(repo_name)
        pr_urls.append(pr_info["url"])

        # Get files
        detail = _gh(
            "pr",
            "view",
            str(number),
            "--repo",
            f"{GH_ORG}/{repo_name}",
            "--json",
            "files,reviews,comments",
        )
        if isinstance(detail, dict):
            for f in detail.get("files") or []:
                path = f.get("path", "")
                if path:
                    all_files.append(f"{repo_name}/{path}")

            # Review comments
            for review in detail.get("reviews") or []:
                body = review.get("body", "").strip()
                if body:
                    all_review_comments.append(
                        {
                            "pr": pr_info["url"],
                            "author": review.get("author", {}).get("login", ""),
                            "body": body,
                            "file": "",
                        }
                    )

            # PR comments (non-review)
            for comment in detail.get("comments") or []:
                body = comment.get("body", "").strip()
                if body:
                    all_review_comments.append(
                        {
                            "pr": pr_info["url"],
                            "author": comment.get("author", {}).get("login", ""),
                            "body": body,
                            "file": "",
                        }
                    )

    # Add repos from commits that had no PRs
    all_repos.update(commit_repos)

    return {
        "repos_changed": sorted(all_repos),
        "files_changed": sorted(set(all_files)),
        "pr_urls": pr_urls,
        "pr_review_comments": all_review_comments,
    }


# ---------------------------------------------------------------------------
# DB insert
# ---------------------------------------------------------------------------


def save_to_db(data: dict, *, force: bool = False) -> None:
    """Insert or replace task_history row."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        if force:
            conn.execute("DELETE FROM task_history WHERE ticket_id = ?", (data["ticket_id"],))

        conn.execute(
            """INSERT INTO task_history (
                ticket_id, ticket_type, summary, description, developer,
                epic_id, parent_id, subtasks, linked_issues, labels,
                components, sprint, story_points, jira_status, status_changelog,
                jira_comments, attachments, repos_changed, files_changed,
                pr_urls, pr_review_comments, bugs_linked, custom_fields
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["ticket_id"],
                data.get("ticket_type", ""),
                data.get("summary", ""),
                data.get("description", ""),
                data.get("developer", ""),
                data.get("epic_id", ""),
                data.get("parent_id", ""),
                json.dumps(data.get("subtasks", [])),
                json.dumps(data.get("linked_issues", {})),
                json.dumps(data.get("labels", [])),
                json.dumps(data.get("components", [])),
                data.get("sprint", ""),
                data.get("story_points"),
                data.get("jira_status", ""),
                json.dumps(data.get("status_changelog", [])),
                json.dumps(data.get("jira_comments", [])),
                json.dumps(data.get("attachments", [])),
                json.dumps(data.get("repos_changed", [])),
                json.dumps(data.get("files_changed", [])),
                json.dumps(data.get("pr_urls", [])),
                json.dumps(data.get("pr_review_comments", [])),
                json.dumps(data.get("bugs_linked", [])),
                json.dumps(data.get("custom_fields", {})),
            ),
        )
        conn.commit()
        print(f"  Saved {data['ticket_id']} to task_history")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def collect(ticket_id: str, *, dry_run: bool = False, force: bool = False) -> dict:
    """Collect full task data from Jira + GitHub."""
    ticket_id = ticket_id.upper().strip()
    print(f"Collecting {ticket_id}...")

    # Jira
    print("  Fetching Jira data...")
    jira_data = fetch_jira(ticket_id)
    print(f"  Jira: {jira_data['ticket_type']} | {jira_data['jira_status']} | {jira_data['summary'][:80]}")

    # GitHub
    print("  Searching GitHub PRs and commits...")
    gh_data = fetch_github_prs(ticket_id)
    print(
        f"  GitHub: {len(gh_data['pr_urls'])} PRs, {len(gh_data['repos_changed'])} repos, {len(gh_data['files_changed'])} files"
    )

    # Merge
    merged = {**jira_data, **gh_data}

    if dry_run:
        print("\n  [dry-run] Would save to DB. Data:")
        print(json.dumps(merged, indent=2, default=str))
    else:
        save_to_db(merged, force=force)

    return merged


def main():
    if len(sys.argv) < 2:
        print("Usage: python collect_task.py <TICKET-ID> [--dry-run] [--force]")
        sys.exit(1)

    ticket_id = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    collect(ticket_id, dry_run=dry_run, force=force)


if __name__ == "__main__":
    main()
