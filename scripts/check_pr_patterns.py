#!/usr/bin/env python3
"""Check a PR's repo list against exported patterns to find missing repos.

Usage:
    python3 check_pr_patterns.py --repos grpc-apm-trustly,grpc-providers-features
"""

import argparse
import json
import sys
from pathlib import Path

PATTERNS_PATH = Path.home() / ".pay-knowledge" / "patterns-export.json"


def load_patterns():
    if not PATTERNS_PATH.exists():
        print(f"ERROR: Patterns file not found at {PATTERNS_PATH}", file=sys.stderr)
        print("Run export_patterns.py first.", file=sys.stderr)
        sys.exit(2)
    return json.loads(PATTERNS_PATH.read_text())


def check(pr_repos: set[str], patterns: dict) -> list[dict]:
    warnings = []

    # 1. Upstream callers: repos frequently missed that are NOT in the PR
    for entry in patterns["upstream_callers"]:
        repo = entry["repo"]
        if repo not in pr_repos:
            warnings.append(
                {
                    "type": "upstream_caller",
                    "repo": repo,
                    "reason": f"Missed in {entry['missed_in_tasks']} tasks (confidence {entry['confidence']:.0%})",
                }
            )

    # 2. Co-occurrences: if any trigger repo is in PR, check missed_repo
    for entry in patterns["co_occurrences"]:
        trigger_repos = set(entry["trigger_repos"])
        if trigger_repos & pr_repos and entry["missed_repo"] not in pr_repos:
            warnings.append(
                {
                    "type": "co_occurrence",
                    "repo": entry["missed_repo"],
                    "reason": (
                        f"When {', '.join(trigger_repos & pr_repos)} changes, "
                        f"{entry['missed_repo']} is also needed ({entry['occurrences']}x seen)"
                    ),
                }
            )

    # 3. Clusters: if repo_a is in PR but repo_b isn't (or vice versa)
    for entry in patterns["clusters"]:
        a, b = entry["repo_a"], entry["repo_b"]
        if a in pr_repos and b not in pr_repos:
            warnings.append(
                {
                    "type": "cluster",
                    "repo": b,
                    "reason": f"Frequently co-missed with {a} ({entry['co_missed_in']}x)",
                }
            )
        elif b in pr_repos and a not in pr_repos:
            warnings.append(
                {
                    "type": "cluster",
                    "repo": a,
                    "reason": f"Frequently co-missed with {b} ({entry['co_missed_in']}x)",
                }
            )

    return warnings


def main():
    parser = argparse.ArgumentParser(description="Check PR repos against known patterns")
    parser.add_argument("--repos", required=True, help="Comma-separated list of repos in the PR")
    args = parser.parse_args()

    pr_repos = {r.strip() for r in args.repos.split(",") if r.strip()}
    if not pr_repos:
        print("ERROR: No repos provided", file=sys.stderr)
        sys.exit(2)

    patterns = load_patterns()
    warnings = check(pr_repos, patterns)

    print(f"PR repos: {', '.join(sorted(pr_repos))}")
    print(f"Patterns version: {patterns['version']} ({patterns['exported_at'][:10]})")
    print()

    if not warnings:
        print("No warnings. All known patterns satisfied.")
        sys.exit(0)

    # Deduplicate by repo, keep most severe
    seen = {}
    for w in warnings:
        repo = w["repo"]
        if repo not in seen:
            seen[repo] = w

    print(f"Found {len(seen)} warning(s):\n")
    for w in seen.values():
        tag = w["type"].upper().replace("_", " ")
        print(f"  [{tag}] {w['repo']}")
        print(f"    {w['reason']}")
        print()

    sys.exit(1)


if __name__ == "__main__":
    main()
