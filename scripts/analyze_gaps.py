#!/usr/bin/env python3
"""
Phase 8, Step 10: Analyze task_gaps for patterns.

Frequency analysis: which repos are most often missed, what gap types dominate,
which edge types reveal the most gaps. Outputs patterns for proactive suggestions.

Usage:
    python scripts/analyze_gaps.py
    python scripts/analyze_gaps.py --save   # save patterns to task_patterns table
"""

import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"


def analyze():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    save = "--save" in sys.argv

    # --- Basic stats ---
    total_tasks = conn.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
    total_gaps = conn.execute("SELECT COUNT(*) FROM task_gaps").fetchone()[0]
    critical = conn.execute("SELECT COUNT(*) FROM task_gaps WHERE gap_type = 'main_flow_repo'").fetchone()[0]
    high = conn.execute(
        "SELECT COUNT(*) FROM task_gaps WHERE confidence >= 0.67 AND gap_type != 'main_flow_repo'"
    ).fetchone()[0]
    low = conn.execute("SELECT COUNT(*) FROM task_gaps WHERE confidence < 0.34").fetchone()[0]

    # Validation-aware stats (only count validated potential_miss as real gaps)
    has_validation = conn.execute("SELECT COUNT(*) FROM task_gaps WHERE validation IS NOT NULL").fetchone()[0] > 0
    if has_validation:
        validated = conn.execute(
            "SELECT validation, COUNT(*) as cnt FROM task_gaps WHERE confidence >= 0.8 GROUP BY validation"
        ).fetchall()
        val_stats = {r["validation"]: r["cnt"] for r in validated}
        real_misses = val_stats.get("potential_miss", 0)
        related_work = val_stats.get("related_work", 0)
        total_validated = real_misses + related_work + val_stats.get("unlinked_pr", 0)
        pollution_rate = related_work * 100 // max(total_validated, 1)
    else:
        real_misses = related_work = pollution_rate = 0

    print(f"=== Gap Analysis ({total_tasks} tasks, {total_gaps} gaps) ===\n")
    print(f"  CRITICAL: {critical}, HIGH: {high}, LOW: {low}")
    if has_validation:
        print(
            f"  Validated (conf>=0.8): {real_misses} real misses, {related_work} related_work ({pollution_rate}% noise)"
        )
    print()

    # --- Most frequently missed repos (validation-aware) ---
    if has_validation:
        print("--- Most frequently missed repos (potential_miss only, conf>=0.5) ---")
        freq = conn.execute("""
            SELECT expected, COUNT(DISTINCT ticket_id) as tasks,
                   GROUP_CONCAT(DISTINCT ticket_id) as tickets,
                   AVG(confidence) as avg_conf,
                   GROUP_CONCAT(DISTINCT gap_type) as types
            FROM task_gaps
            WHERE confidence >= 0.5 AND validation = 'potential_miss'
            GROUP BY expected
            ORDER BY tasks DESC, avg_conf DESC
            LIMIT 20
        """).fetchall()
    else:
        print("--- Most frequently missed repos (CRITICAL+HIGH) ---")
        freq = conn.execute("""
            SELECT expected, COUNT(DISTINCT ticket_id) as tasks,
                   GROUP_CONCAT(DISTINCT ticket_id) as tickets,
                   AVG(confidence) as avg_conf,
                   GROUP_CONCAT(DISTINCT gap_type) as types
            FROM task_gaps
            WHERE confidence >= 0.5
            GROUP BY expected
            ORDER BY tasks DESC, avg_conf DESC
            LIMIT 20
        """).fetchall()
    for r in freq:
        print(f"  {r['expected']:45s} | {r['tasks']} tasks | conf={r['avg_conf']:.2f} | {r['tickets']}")

    # --- Gap type distribution ---
    print("\n--- Gap type distribution ---")
    types = conn.execute("""
        SELECT gap_type, COUNT(*) as cnt, COUNT(DISTINCT ticket_id) as tasks
        FROM task_gaps GROUP BY gap_type ORDER BY cnt DESC
    """).fetchall()
    for r in types:
        print(f"  {r['gap_type']:25s} | {r['cnt']:3d} gaps across {r['tasks']} tasks")

    # --- Root cause distribution ---
    print("\n--- Root cause distribution ---")
    causes = conn.execute("""
        SELECT root_cause, COUNT(*) as cnt
        FROM task_gaps GROUP BY root_cause ORDER BY cnt DESC
    """).fetchall()
    for r in causes:
        print(f"  {r['root_cause']:55s} | {r['cnt']:3d} gaps")

    # --- Strategy effectiveness ---
    print("\n--- Strategy effectiveness (which strategies find gaps) ---")
    rows = conn.execute("SELECT found_by FROM task_gaps WHERE confidence >= 0.5").fetchall()
    strategy_counter: Counter = Counter()
    for r in rows:
        for s in r["found_by"].split(","):
            s = s.strip()
            if s:
                strategy_counter[s] += 1
    for strategy, count in strategy_counter.most_common():
        print(f"  {strategy:20s} | {count:3d} high-confidence gaps")

    # --- Per-task summary ---
    print("\n--- Per-task summary ---")
    tasks = conn.execute("""
        SELECT t.ticket_id, t.summary,
               COUNT(g.id) as total_gaps,
               SUM(CASE WHEN g.gap_type = 'main_flow_repo' THEN 1 ELSE 0 END) as critical,
               SUM(CASE WHEN g.confidence >= 0.67 AND g.gap_type != 'main_flow_repo' THEN 1 ELSE 0 END) as high,
               json_array_length(t.repos_changed) as known_repos
        FROM task_history t
        LEFT JOIN task_gaps g ON g.ticket_id = t.ticket_id
        GROUP BY t.ticket_id
        ORDER BY t.ticket_id
    """).fetchall()
    for r in tasks:
        print(
            f"  {r['ticket_id']:8s} | {r['known_repos']:2d} repos | {r['critical']:2d} CRIT {r['high']:2d} HIGH {r['total_gaps']:3d} total | {r['summary'][:50]}"
        )

    # --- Patterns: repo co-occurrence ---
    print("\n--- Patterns: when repo X changes, repo Y is often missed ---")
    # For each CRITICAL gap, find which known repos co-occur
    # Validation-aware: only count potential_miss gaps (exclude related_work noise)
    co_occ_filter = "AND g.validation = 'potential_miss'" if has_validation else ""
    patterns = conn.execute(f"""
        SELECT g.expected as missed_repo,
               t.repos_changed,
               COUNT(DISTINCT g.ticket_id) as occurrences
        FROM task_gaps g
        JOIN task_history t ON t.ticket_id = g.ticket_id
        WHERE g.gap_type = 'main_flow_repo' {co_occ_filter}
        GROUP BY g.expected
        HAVING occurrences >= 2
        ORDER BY occurrences DESC
    """).fetchall()

    pattern_list = []
    for p in patterns:
        missed = p["missed_repo"]
        # Find common repos across all tasks where this gap appears
        gap_tasks = conn.execute(
            "SELECT t.repos_changed FROM task_gaps g JOIN task_history t ON t.ticket_id = g.ticket_id WHERE g.expected = ?",
            (missed,),
        ).fetchall()
        repo_counter: Counter = Counter()
        for gt in gap_tasks:
            for repo in json.loads(gt["repos_changed"]):
                repo_counter[repo] += 1
        # Repos that appear in ALL tasks where this gap occurs
        common = [repo for repo, cnt in repo_counter.items() if cnt >= p["occurrences"]]
        if common:
            pattern_list.append(
                {
                    "missed_repo": missed,
                    "when_changed": common,
                    "occurrences": p["occurrences"],
                }
            )
            print(f"  When changing [{', '.join(common[:3])}] → also check {missed} ({p['occurrences']} times)")

    # --- Pattern: upstream callers (repos missed as main_flow across many tasks) ---
    # Validation-aware: compute miss_ratio to filter out noise from related_work
    print("\n--- Patterns: upstream caller frequency ---")
    if has_validation:
        upstream_patterns = conn.execute("""
            SELECT expected,
                   COUNT(DISTINCT ticket_id) as tasks,
                   AVG(confidence) as avg_conf,
                   SUM(CASE WHEN validation='potential_miss' THEN 1 ELSE 0 END) as real_misses,
                   SUM(CASE WHEN validation='related_work' THEN 1 ELSE 0 END) as related,
                   ROUND(SUM(CASE WHEN validation='potential_miss' THEN 1.0 ELSE 0 END) / COUNT(*), 2) as miss_ratio
            FROM task_gaps
            WHERE gap_type = 'main_flow_repo' AND confidence >= 0.5
            GROUP BY expected
            HAVING real_misses >= 2
            ORDER BY real_misses DESC
        """).fetchall()
        print("  (filtered: miss_ratio = fraction of validated potential_miss, related_work excluded)")
    else:
        upstream_patterns = conn.execute("""
            SELECT expected, COUNT(DISTINCT ticket_id) as tasks, AVG(confidence) as avg_conf,
                   0 as real_misses, 0 as related, 1.0 as miss_ratio
            FROM task_gaps
            WHERE gap_type = 'main_flow_repo'
            GROUP BY expected
            HAVING tasks >= 3
            ORDER BY tasks DESC
        """).fetchall()

    upstream_list = []
    for r in upstream_patterns:
        miss_ratio = r["miss_ratio"] if has_validation else 1.0
        real = r["real_misses"] if has_validation else r["tasks"]
        rel = r["related"] if has_validation else 0
        label = f"| {real} real, {rel} related, ratio={miss_ratio:.0%}" if has_validation else ""
        print(f"  {r['expected']:45s} | missed in {r['tasks']} tasks | avg_conf={r['avg_conf']:.2f} {label}")
        upstream_list.append(
            {
                "missed_repo": r["expected"],
                "occurrences": real if has_validation else r["tasks"],
                "confidence": r["avg_conf"],
                "miss_ratio": miss_ratio,
            }
        )

    # --- Pattern: project-specific gaps ---
    print("\n--- Patterns: project-specific top gaps ---")
    proj_filter = "AND g.validation = 'potential_miss'" if has_validation else ""
    for project in ["PI", "CORE", "BO", "HS"]:
        proj_gaps = conn.execute(
            f"""
            SELECT g.expected, COUNT(DISTINCT g.ticket_id) as tasks
            FROM task_gaps g
            WHERE g.ticket_id LIKE ? AND g.confidence >= 0.5 {proj_filter}
            GROUP BY g.expected
            HAVING tasks >= 3
            ORDER BY tasks DESC
            LIMIT 5
        """,
            (f"{project}-%",),
        ).fetchall()
        if proj_gaps:
            top = ", ".join(f"{r['expected']}({r['tasks']})" for r in proj_gaps)
            print(f"  {project:5s}: {top}")

    # --- Pattern: gap clusters (repos that are missed TOGETHER) ---
    print("\n--- Patterns: gap clusters (repos missed together) ---")
    cluster_filter = "AND validation = 'potential_miss'" if has_validation else ""
    cluster_data = conn.execute(f"""
        SELECT ticket_id, GROUP_CONCAT(expected, '|') as missed
        FROM task_gaps
        WHERE confidence >= 0.5 {cluster_filter}
        GROUP BY ticket_id
        HAVING COUNT(*) >= 2
    """).fetchall()
    pair_counter: Counter = Counter()
    for row in cluster_data:
        missed = sorted(row["missed"].split("|"))
        for i in range(len(missed)):
            for j in range(i + 1, len(missed)):
                pair_counter[(missed[i], missed[j])] += 1
    print("  Top co-missed pairs:")
    for (a, b), cnt in pair_counter.most_common(10):
        print(f"    {a} + {b} | {cnt} tasks")

    # --- Pattern: task-size correlation ---
    print("\n--- Patterns: task size vs gap count ---")
    size_corr = conn.execute("""
        SELECT
            CASE
                WHEN json_array_length(t.repos_changed) <= 2 THEN '1-2 repos'
                WHEN json_array_length(t.repos_changed) <= 5 THEN '3-5 repos'
                WHEN json_array_length(t.repos_changed) <= 10 THEN '6-10 repos'
                ELSE '11+ repos'
            END as task_size,
            COUNT(DISTINCT t.ticket_id) as tasks,
            ROUND(AVG(gap_count), 1) as avg_gaps,
            ROUND(AVG(crit_count), 1) as avg_crit
        FROM task_history t
        LEFT JOIN (
            SELECT ticket_id, COUNT(*) as gap_count,
                   SUM(CASE WHEN gap_type='main_flow_repo' THEN 1 ELSE 0 END) as crit_count
            FROM task_gaps GROUP BY ticket_id
        ) g ON g.ticket_id = t.ticket_id
        GROUP BY task_size
        ORDER BY task_size
    """).fetchall()
    for r in size_corr:
        print(
            f"  {r['task_size']:12s} | {r['tasks']:2d} tasks | avg {r['avg_gaps']} gaps | avg {r['avg_crit']} critical"
        )

    # --- Validation quality report ---
    if has_validation:
        print("\n--- Validation quality: repos with worst miss_ratio (most related_work noise) ---")
        noisy = conn.execute("""
            SELECT expected,
                   COUNT(*) as total,
                   SUM(CASE WHEN validation='potential_miss' THEN 1 ELSE 0 END) as real,
                   SUM(CASE WHEN validation='related_work' THEN 1 ELSE 0 END) as noise,
                   ROUND(SUM(CASE WHEN validation='potential_miss' THEN 1.0 ELSE 0 END) / COUNT(*), 2) as miss_ratio
            FROM task_gaps
            WHERE confidence >= 0.8 AND validation IS NOT NULL
            GROUP BY expected
            HAVING total >= 5 AND noise >= 3
            ORDER BY miss_ratio ASC
            LIMIT 10
        """).fetchall()
        for r in noisy:
            print(
                f"  {r['expected']:45s} | {r['real']}/{r['total']} real ({r['miss_ratio']:.0%}) | {r['noise']} related_work"
            )

    # --- Save patterns if requested ---
    if save:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern_type TEXT,
                missed_repo TEXT,
                trigger_repos TEXT,
                occurrences INTEGER,
                confidence REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("DELETE FROM task_patterns")

        # Save co_occurrence patterns
        for p in pattern_list:
            conn.execute(
                "INSERT INTO task_patterns (pattern_type, missed_repo, trigger_repos, occurrences, confidence) VALUES (?, ?, ?, ?, ?)",
                (
                    "co_occurrence",
                    p["missed_repo"],
                    json.dumps(p["when_changed"]),
                    p["occurrences"],
                    min(1.0, p["occurrences"] / total_tasks),
                ),
            )

        # Save upstream_caller patterns (weighted by miss_ratio when validation exists)
        for p in upstream_list:
            # Discount confidence by miss_ratio: patterns with lots of related_work get lower confidence
            effective_conf = p["confidence"] * p.get("miss_ratio", 1.0)
            conn.execute(
                "INSERT INTO task_patterns (pattern_type, missed_repo, trigger_repos, occurrences, confidence) VALUES (?, ?, ?, ?, ?)",
                ("upstream_caller", p["missed_repo"], "[]", p["occurrences"], round(effective_conf, 3)),
            )

        # Save top co-missed pairs as cluster patterns
        min_cluster = 3 if has_validation else 5  # lower threshold when pre-filtered by validation
        for (a, b), cnt in pair_counter.most_common(15):
            if cnt >= min_cluster:
                conn.execute(
                    "INSERT INTO task_patterns (pattern_type, missed_repo, trigger_repos, occurrences, confidence) VALUES (?, ?, ?, ?, ?)",
                    ("cluster", a, json.dumps([b]), cnt, min(1.0, cnt / total_tasks)),
                )

        conn.commit()
        total_saved = conn.execute("SELECT COUNT(*) FROM task_patterns").fetchone()[0]
        print(f"\n  Saved {total_saved} patterns to task_patterns (co_occurrence + upstream_caller + cluster)")

    conn.close()


if __name__ == "__main__":
    analyze()
