#!/usr/bin/env python3
"""Mine co-change rules from task_history.

Finds repos that frequently change together across tasks. Outputs candidate
rules for conventions.yaml co_change_rules section.

Usage:
    python scripts/mine_co_changes.py [--min-support 3] [--min-confidence 0.5]
    python scripts/mine_co_changes.py --pi-only   # only PI-relevant repos
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "knowledge.db"

PI_KEYWORDS = [
    "apm-", "providers-", "payment-gateway", "webhooks", "express-api",
    "e2e-tests", "libs-types", "core-schemas", "core-transactions",
    "cdc-tests", "core-settings", "risk-engine", "risk-rules",
]


def is_pi_relevant(repo: str) -> bool:
    return any(k in repo for k in PI_KEYWORDS)


def mine(min_support: int = 3, min_confidence: float = 0.5, pi_only: bool = False):
    db = sqlite3.connect(str(DB_PATH))
    rows = db.execute(
        "SELECT ticket_id, repos_changed FROM task_history "
        "WHERE repos_changed IS NOT NULL AND repos_changed != ''"
    ).fetchall()

    tasks = []
    for tid, rj in rows:
        try:
            repos = json.loads(rj)
            if isinstance(repos, list) and len(repos) >= 2:
                tasks.append((tid, repos))
        except (json.JSONDecodeError, TypeError):
            pass

    pair_counts: Counter[tuple[str, str]] = Counter()
    repo_counts: Counter[str] = Counter()

    for _, repos in tasks:
        for r in repos:
            repo_counts[r] += 1
        for i, r1 in enumerate(repos):
            for r2 in repos[i + 1 :]:
                pair = tuple(sorted([r1, r2]))
                pair_counts[pair] += 1

    print(f"Tasks with 2+ repos: {len(tasks)}")
    print(f"Unique repos: {len(repo_counts)}")
    print(f"Unique pairs: {len(pair_counts)}")
    print()

    rules = []
    for (r1, r2), count in pair_counts.most_common():
        if count < min_support:
            continue
        if pi_only and not (is_pi_relevant(r1) or is_pi_relevant(r2)):
            continue

        p1 = count / repo_counts[r1]  # P(r2|r1)
        p2 = count / repo_counts[r2]  # P(r1|r2)

        if max(p1, p2) >= min_confidence:
            rules.append((r1, r2, count, p1, p2))

    # Output as YAML-ready format
    rule_map: dict[str, list[str]] = {}
    for r1, r2, count, p1, p2 in rules:
        if p1 >= min_confidence:
            rule_map.setdefault(r1, []).append(r2)
        if p2 >= min_confidence:
            rule_map.setdefault(r2, []).append(r1)

    print(f"Rules (support >= {min_support}, confidence >= {min_confidence:.0%}):")
    print()
    for trigger in sorted(rule_map):
        companions = sorted(set(rule_map[trigger]))
        print(f'  {trigger}: {json.dumps(companions)}')

    print(f"\nTotal triggers: {len(rule_map)}")
    print(f"Total rules: {sum(len(v) for v in rule_map.values())}")


if __name__ == "__main__":
    min_support = 3
    min_confidence = 0.5
    pi_only = False

    args = sys.argv[1:]
    if "--min-support" in args:
        idx = args.index("--min-support")
        min_support = int(args[idx + 1])
    if "--min-confidence" in args:
        idx = args.index("--min-confidence")
        min_confidence = float(args[idx + 1])
    if "--pi-only" in args:
        pi_only = True

    mine(min_support, min_confidence, pi_only)
