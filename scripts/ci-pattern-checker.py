#!/usr/bin/env python3
"""
CI Pattern Checker — scans recent pay-com PRs against learned patterns.

Runs locally via launchd (like auto_collect.py). Finds recently merged PRs,
checks if commonly co-changed repos are missing, and optionally posts
a GitHub comment on the PR.

Usage:
    python3 scripts/ci-pattern-checker.py                    # scan last 24h, log only
    python3 scripts/ci-pattern-checker.py --hours=48         # scan last 48h
    python3 scripts/ci-pattern-checker.py --comment          # post PR comments
    python3 scripts/ci-pattern-checker.py --pr=1234 --repo=grpc-apm-trustly  # single PR
    python3 scripts/ci-pattern-checker.py --developer=vladislav  # filter by developer
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".pay-knowledge"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"
PATTERNS_PATH = _BASE_DIR / "patterns-export.json"
LOG_DIR = _BASE_DIR / "logs"
GH = "/opt/homebrew/bin/gh"
ORG = "pay-com"


def gh_api(endpoint: str, method: str = "GET", jq: str = "") -> str:
    """Call GitHub API via gh CLI."""
    cmd = [GH, "api", endpoint, "--method", method]
    if jq:
        cmd.extend(["--jq", jq])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def find_recent_prs(hours: int = 24, developer: str | None = None) -> list[dict]:
    """Find recently merged PRs in pay-com org."""
    since = (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f"org:{ORG} is:pr is:merged merged:>{since}"
    if developer:
        query += f" author:{developer}"

    # GitHub search API (paginated, max 100 per page)
    jq_filter = (
        "[.items[] | {number: .number, title: .title, repo: .repository_url, user: .user.login, url: .html_url}]"
    )
    raw = gh_api(f"search/issues?q={query.replace(' ', '+')}&per_page=100&sort=updated", jq=jq_filter)

    if not raw or raw.startswith("ERROR"):
        print(f"  [warn] GitHub API error: {raw}")
        return []

    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        print("  [warn] Failed to parse GitHub response")
        return []

    # Extract repo name from repository_url
    for pr in prs:
        repo_url = pr.get("repo", "")
        pr["repo_name"] = repo_url.split("/")[-1] if repo_url else ""

    return prs


def get_pr_changed_files(repo_name: str, pr_number: int) -> list[str]:
    """Get files changed in a PR."""
    jq_filter = "[.[].filename]"
    raw = gh_api(f"repos/{ORG}/{repo_name}/pulls/{pr_number}/files?per_page=100", jq=jq_filter)
    try:
        return json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []


def group_prs_by_developer(prs: list[dict]) -> dict[str, list[dict]]:
    """Group PRs by developer to find cross-repo task patterns."""
    groups: dict[str, list[dict]] = {}
    for pr in prs:
        dev = pr.get("user", "unknown")
        groups.setdefault(dev, []).append(pr)
    return groups


def load_patterns() -> dict:
    """Load exported patterns JSON."""
    if not PATTERNS_PATH.exists():
        # Try to generate
        export_script = _BASE_DIR / "scripts" / "export_patterns.py"
        if export_script.exists():
            subprocess.run([sys.executable, str(export_script)], capture_output=True)
    if PATTERNS_PATH.exists():
        return json.loads(PATTERNS_PATH.read_text())
    return {}


def check_patterns(pr_repos: set[str], patterns: dict) -> list[dict]:
    """Check repos against patterns, return warnings."""
    warnings = []

    # Upstream callers — only trigger if PR touches payment-flow repos
    PAYMENT_FLOW_PREFIXES = (
        "grpc-apm-",
        "grpc-payment-",
        "grpc-providers-",
        "workflow-provider-",
        "express-api-v1",
        "express-api-webhook",
        "express-api-internal",
        "grpc-core-",
    )
    pr_in_payment_flow = any(any(repo.startswith(pfx) for pfx in PAYMENT_FLOW_PREFIXES) for repo in pr_repos)
    if pr_in_payment_flow:
        for entry in patterns.get("upstream_callers", []):
            repo = entry["repo"]
            if repo not in pr_repos and entry["missed_in_tasks"] >= 10:
                warnings.append(
                    {
                        "type": "upstream_caller",
                        "repo": repo,
                        "severity": "high" if entry["missed_in_tasks"] >= 15 else "medium",
                        "reason": f"Missed in {entry['missed_in_tasks']} past tasks (confidence {entry['confidence']:.0%})",
                    }
                )

    # Co-occurrences
    for entry in patterns.get("co_occurrences", []):
        trigger_repos = set(entry["trigger_repos"])
        matched = trigger_repos & pr_repos
        if matched and entry["missed_repo"] not in pr_repos and entry["occurrences"] >= 5:
            warnings.append(
                {
                    "type": "co_occurrence",
                    "repo": entry["missed_repo"],
                    "severity": "high" if entry["occurrences"] >= 10 else "medium",
                    "reason": f"When {', '.join(matched)} changes → also check {entry['missed_repo']} ({entry['occurrences']}x)",
                }
            )

    # Clusters
    for entry in patterns.get("clusters", []):
        a, b = entry["repo_a"], entry["repo_b"]
        if a in pr_repos and b not in pr_repos and entry["co_missed_in"] >= 5:
            warnings.append(
                {
                    "type": "cluster",
                    "repo": b,
                    "severity": "medium",
                    "reason": f"Frequently co-missed with {a} ({entry['co_missed_in']}x)",
                }
            )
        elif b in pr_repos and a not in pr_repos and entry["co_missed_in"] >= 5:
            warnings.append(
                {
                    "type": "cluster",
                    "repo": a,
                    "severity": "medium",
                    "reason": f"Frequently co-missed with {b} ({entry['co_missed_in']}x)",
                }
            )

    # Deduplicate by repo (keep highest severity)
    seen: dict[str, dict] = {}
    for w in warnings:
        repo = w["repo"]
        if repo not in seen or (w["severity"] == "high" and seen[repo]["severity"] != "high"):
            seen[repo] = w

    return list(seen.values())


def format_pr_comment(dev: str, repos: set[str], warnings: list[dict]) -> str:
    """Format a GitHub PR comment with pattern warnings."""
    high = [w for w in warnings if w["severity"] == "high"]
    medium = [w for w in warnings if w["severity"] == "medium"]

    lines = [
        "## 🔍 Pattern Check",
        "",
        f"Based on **{len(repos)}** repos in recent PRs by `{dev}`:",
        "",
    ]

    if high:
        lines.append("### ⚠️ High Priority")
        for w in high:
            lines.append(f"- **{w['repo']}** — {w['reason']}")
        lines.append("")

    if medium:
        lines.append("### 📋 Worth Checking")
        for w in medium[:5]:  # limit to 5 medium
            lines.append(f"- **{w['repo']}** — {w['reason']}")
        lines.append("")

    lines.append("_Auto-generated by pay-knowledge pattern checker._")
    return "\n".join(lines)


def post_pr_comment(repo_name: str, pr_number: int, body: str):
    """DISABLED — pay-com is read-only. Never post comments automatically.
    All output goes to local logs/reports only. If needed in the future,
    this block must be explicitly re-enabled with a safety review."""
    raise RuntimeError("BLOCKED: posting comments to pay-com is disabled. Read-only mode.")


def save_log(results: list[dict]):
    """Save check results to log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "pattern-checker.jsonl"
    with open(log_file, "a") as f:
        for r in results:
            r["checked_at"] = datetime.now(UTC).isoformat()
            f.write(json.dumps(r) + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Check recent PRs against learned patterns")
    parser.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")
    parser.add_argument("--comment", action="store_true", help="DISABLED — read-only mode")
    parser.add_argument("--developer", type=str, help="Filter by developer username")
    parser.add_argument("--pr", type=int, help="Check single PR number")
    parser.add_argument("--repo", type=str, help="Repo name for --pr mode")
    parser.add_argument("--dry-run", action="store_true", help="Print but don't post comments")
    args = parser.parse_args()

    print(f"=== Pattern Checker: {ORG} PRs (last {args.hours}h) ===\n")

    patterns = load_patterns()
    if not patterns:
        print("  ERROR: No patterns found. Run export_patterns.py first.")
        sys.exit(1)

    print(f"  Patterns: {patterns['stats']['total_patterns']} (from {patterns['stats']['total_tasks']} tasks)\n")

    # Single PR mode
    if args.pr and args.repo:
        files = get_pr_changed_files(args.repo, args.pr)
        pr_repos = {args.repo}
        warnings = check_patterns(pr_repos, patterns)
        print(f"  PR #{args.pr} in {args.repo}: {len(files)} files changed")
        if warnings:
            print(f"  {len(warnings)} warning(s):")
            for w in warnings:
                print(f"    [{w['severity'].upper()}] {w['repo']} — {w['reason']}")
        else:
            print("  No pattern warnings.")
        return

    # Scan recent PRs
    prs = find_recent_prs(args.hours, args.developer)
    if not prs:
        print("  No recent merged PRs found.")
        return

    print(f"  Found {len(prs)} merged PRs\n")

    # Group by developer
    dev_groups = group_prs_by_developer(prs)
    results = []

    for dev, dev_prs in dev_groups.items():
        dev_repos = {pr["repo_name"] for pr in dev_prs}
        warnings = check_patterns(dev_repos, patterns)

        result = {
            "developer": dev,
            "pr_count": len(dev_prs),
            "repos": sorted(dev_repos),
            "warnings": len(warnings),
            "high": sum(1 for w in warnings if w["severity"] == "high"),
        }
        results.append(result)

        if warnings:
            print(f"  {dev} ({len(dev_prs)} PRs, {len(dev_repos)} repos):")
            for w in warnings:
                icon = "⚠️" if w["severity"] == "high" else "📋"
                print(f"    {icon} {w['repo']} — {w['reason']}")

            # Comment posting is BLOCKED — pay-com is read-only
            if args.comment:
                print("    ⛔ --comment is disabled. pay-com is read-only.")
            elif args.dry_run and warnings:
                comment = format_pr_comment(dev, dev_repos, warnings)
                print(f"\n    [DRY RUN] Would post:\n{comment}\n")

            print()

    # Summary
    total_warnings = sum(r["warnings"] for r in results)
    total_high = sum(r["high"] for r in results)
    print(
        f"--- Summary: {len(prs)} PRs, {len(dev_groups)} developers, {total_warnings} warnings ({total_high} high) ---"
    )

    save_log(results)


if __name__ == "__main__":
    main()
