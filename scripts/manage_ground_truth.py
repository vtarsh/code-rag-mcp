#!/usr/bin/env python3
"""
Ground truth management for gap detection calibration.

Stores manual and automated review verdicts so the system can learn
which gaps are real (true positives) vs noise (false positives).

Usage:
    python3 manage_ground_truth.py --import-audit
    python3 manage_ground_truth.py --stats
    python3 manage_ground_truth.py --calibrate
    python3 manage_ground_truth.py --review PI-54 grpc-payment-gateway false_positive "not needed"
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "knowledge.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ground_truth (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT,
            expected_repo TEXT,
            verdict TEXT,
            reason TEXT,
            reviewer TEXT,
            reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticket_id, expected_repo)
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# --import-audit: bulk import from our earlier manual audits
# ---------------------------------------------------------------------------

AUDIT_RULES = [
    {
        "repo": "express-webhooks",
        "ticket_filter": None,  # all gaps for this repo
        "verdict": "false_positive",
        "reason": "dormant repo, 22 commits lifetime",
        "reviewer": "manual:audit_2026-03",
    },
    {
        "repo": "grpc-payment-gateway",
        "ticket_filter": "PI-%",  # only PI tickets
        "verdict": "false_positive",
        "reason": "provider-agnostic orchestrator, no provider-specific code",
        "reviewer": "manual:audit_2026-03",
    },
    # These repos had no gaps in the DB, but record the rule for completeness.
    # If gaps appear later they'll be caught by future imports.
    {
        "repo": "workflow-sub-payments-master",
        "ticket_filter": None,
        "verdict": "false_positive",
        "reason": "static repo, 20 commits lifetime",
        "reviewer": "manual:audit_2026-03",
    },
    {
        "repo": "workflow-subscriptions-manager",
        "ticket_filter": None,
        "verdict": "false_positive",
        "reason": "static repo",
        "reviewer": "manual:audit_2026-03",
    },
]


def import_audit(conn):
    ensure_table(conn)
    total_imported = 0
    total_skipped = 0

    for rule in AUDIT_RULES:
        repo = rule["repo"]
        ticket_filter = rule["ticket_filter"]

        # Find matching gaps in task_gaps
        if ticket_filter:
            rows = conn.execute(
                "SELECT ticket_id, expected FROM task_gaps WHERE expected = ? AND ticket_id LIKE ?",
                (repo, ticket_filter),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ticket_id, expected FROM task_gaps WHERE expected = ?",
                (repo,),
            ).fetchall()

        if not rows:
            print(
                f"  [{repo}] No matching gaps in task_gaps" + (f" (filter: {ticket_filter})" if ticket_filter else "")
            )
            continue

        for row in rows:
            try:
                conn.execute(
                    """INSERT INTO ground_truth (ticket_id, expected_repo, verdict, reason, reviewer)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["ticket_id"], repo, rule["verdict"], rule["reason"], rule["reviewer"]),
                )
                total_imported += 1
            except sqlite3.IntegrityError:
                total_skipped += 1

        print(f"  [{repo}] {len(rows)} gaps matched" + (f" (filter: {ticket_filter})" if ticket_filter else ""))

    conn.commit()
    print(f"\nImported: {total_imported}, Skipped (already exist): {total_skipped}")


# ---------------------------------------------------------------------------
# --stats: precision analysis comparing task_gaps.validation vs ground_truth
# ---------------------------------------------------------------------------


def show_stats(conn):
    ensure_table(conn)

    total_gt = conn.execute("SELECT COUNT(*) FROM ground_truth").fetchone()[0]
    if total_gt == 0:
        print("No ground truth entries yet. Run --import-audit first.")
        return

    print("=== Ground Truth Stats ===\n")
    print(f"Total reviewed: {total_gt}")

    # Verdict distribution
    rows = conn.execute(
        "SELECT verdict, COUNT(*) as cnt FROM ground_truth GROUP BY verdict ORDER BY cnt DESC"
    ).fetchall()
    print("\nVerdict distribution:")
    for r in rows:
        pct = r["cnt"] / total_gt * 100
        print(f"  {r['verdict']:20s} {r['cnt']:4d}  ({pct:.1f}%)")

    # Precision: how many of our detected gaps are actually true positives?
    # Join ground_truth with task_gaps to see overlap
    joined = conn.execute("""
        SELECT
            gt.verdict,
            tg.validation,
            tg.confidence,
            tg.found_by,
            tg.gap_type,
            gt.ticket_id,
            gt.expected_repo
        FROM ground_truth gt
        JOIN task_gaps tg ON gt.ticket_id = tg.ticket_id AND gt.expected_repo = tg.expected
    """).fetchall()

    if not joined:
        print("\nNo overlap between ground_truth and task_gaps (column mismatch?).")
        return

    print(f"\nJoined with task_gaps: {len(joined)} entries")

    # Precision by validation status
    by_validation = defaultdict(lambda: {"tp": 0, "fp": 0, "unc": 0})
    for r in joined:
        val = r["validation"] or "(none)"
        if r["verdict"] == "true_positive":
            by_validation[val]["tp"] += 1
        elif r["verdict"] == "false_positive":
            by_validation[val]["fp"] += 1
        else:
            by_validation[val]["unc"] += 1

    print("\nPrecision by task_gaps.validation:")
    print(f"  {'validation':20s} {'TP':>5s} {'FP':>5s} {'UNC':>5s} {'Precision':>10s}")
    print(f"  {'-' * 20} {'-' * 5} {'-' * 5} {'-' * 5} {'-' * 10}")
    for val, counts in sorted(by_validation.items()):
        total = counts["tp"] + counts["fp"]
        precision = f"{counts['tp'] / total * 100:.1f}%" if total > 0 else "N/A"
        print(f"  {val:20s} {counts['tp']:5d} {counts['fp']:5d} {counts['unc']:5d} {precision:>10s}")

    # Precision by gap_type
    by_gap_type = defaultdict(lambda: {"tp": 0, "fp": 0, "unc": 0})
    for r in joined:
        gt = r["gap_type"] or "(none)"
        if r["verdict"] == "true_positive":
            by_gap_type[gt]["tp"] += 1
        elif r["verdict"] == "false_positive":
            by_gap_type[gt]["fp"] += 1
        else:
            by_gap_type[gt]["unc"] += 1

    print("\nPrecision by gap_type:")
    print(f"  {'gap_type':20s} {'TP':>5s} {'FP':>5s} {'UNC':>5s} {'Precision':>10s}")
    print(f"  {'-' * 20} {'-' * 5} {'-' * 5} {'-' * 5} {'-' * 10}")
    for gt, counts in sorted(by_gap_type.items()):
        total = counts["tp"] + counts["fp"]
        precision = f"{counts['tp'] / total * 100:.1f}%" if total > 0 else "N/A"
        print(f"  {gt:20s} {counts['tp']:5d} {counts['fp']:5d} {counts['unc']:5d} {precision:>10s}")

    # Overall
    total_tp = sum(c["tp"] for c in by_validation.values())
    total_fp = sum(c["fp"] for c in by_validation.values())
    total_all = total_tp + total_fp
    overall_precision = f"{total_tp / total_all * 100:.1f}%" if total_all > 0 else "N/A"
    print(f"\nOverall precision (TP / (TP + FP)): {overall_precision}  ({total_tp} TP, {total_fp} FP)")


# ---------------------------------------------------------------------------
# --calibrate: compare confidence scores against ground truth
# ---------------------------------------------------------------------------


def calibrate(conn):
    ensure_table(conn)

    joined = conn.execute("""
        SELECT
            gt.verdict,
            tg.confidence,
            tg.found_by,
            tg.gap_type,
            gt.expected_repo
        FROM ground_truth gt
        JOIN task_gaps tg ON gt.ticket_id = tg.ticket_id AND gt.expected_repo = tg.expected
    """).fetchall()

    if not joined:
        print("No ground truth data joined with task_gaps. Run --import-audit first.")
        return

    print("=== Calibration Report ===\n")

    # 1. Precision by confidence bucket
    buckets = {"0.0-0.3": [], "0.3-0.5": [], "0.5-0.7": [], "0.7-0.9": [], "0.9-1.0": []}
    for r in joined:
        c = r["confidence"]
        if c < 0.3:
            buckets["0.0-0.3"].append(r)
        elif c < 0.5:
            buckets["0.3-0.5"].append(r)
        elif c < 0.7:
            buckets["0.5-0.7"].append(r)
        elif c < 0.9:
            buckets["0.7-0.9"].append(r)
        else:
            buckets["0.9-1.0"].append(r)

    print("1. Precision by confidence bucket:")
    print(f"   {'Bucket':12s} {'Total':>6s} {'TP':>5s} {'FP':>5s} {'Precision':>10s} {'Action':>20s}")
    print(f"   {'-' * 12} {'-' * 6} {'-' * 5} {'-' * 5} {'-' * 10} {'-' * 20}")
    for bucket, items in buckets.items():
        tp = sum(1 for r in items if r["verdict"] == "true_positive")
        fp = sum(1 for r in items if r["verdict"] == "false_positive")
        total = tp + fp
        if total == 0:
            precision_str = "N/A"
            action = ""
        else:
            precision = tp / total * 100
            precision_str = f"{precision:.1f}%"
            if precision < 20:
                action = "SUPPRESS (high FP)"
            elif precision < 50:
                action = "lower confidence"
            elif precision > 80:
                action = "keep / boost"
            else:
                action = "review manually"
        print(f"   {bucket:12s} {total:6d} {tp:5d} {fp:5d} {precision_str:>10s} {action:>20s}")

    # 2. Precision by found_by strategy
    strategy_stats = defaultdict(lambda: {"tp": 0, "fp": 0})
    for r in joined:
        strategies = [s.strip() for s in (r["found_by"] or "").split(",")]
        verdict = r["verdict"]
        for s in strategies:
            if not s:
                continue
            if verdict == "true_positive":
                strategy_stats[s]["tp"] += 1
            elif verdict == "false_positive":
                strategy_stats[s]["fp"] += 1

    print("\n2. Precision by found_by strategy:")
    print(f"   {'Strategy':25s} {'TP':>5s} {'FP':>5s} {'Precision':>10s} {'Suggestion':>25s}")
    print(f"   {'-' * 25} {'-' * 5} {'-' * 5} {'-' * 10} {'-' * 25}")
    for strategy, counts in sorted(strategy_stats.items(), key=lambda x: -(x[1]["tp"] + x[1]["fp"])):
        total = counts["tp"] + counts["fp"]
        if total == 0:
            continue
        precision = counts["tp"] / total * 100
        if precision < 10:
            suggestion = "DISABLE or heavy penalty"
        elif precision < 30:
            suggestion = "reduce weight significantly"
        elif precision < 50:
            suggestion = "reduce weight"
        elif precision > 80:
            suggestion = "high signal, keep"
        else:
            suggestion = "neutral"
        print(f"   {strategy:25s} {counts['tp']:5d} {counts['fp']:5d} {precision:>9.1f}% {suggestion:>25s}")

    # 3. Per-repo precision
    repo_stats = defaultdict(lambda: {"tp": 0, "fp": 0})
    for r in joined:
        repo = r["expected_repo"]
        if r["verdict"] == "true_positive":
            repo_stats[repo]["tp"] += 1
        elif r["verdict"] == "false_positive":
            repo_stats[repo]["fp"] += 1

    print("\n3. Per-repo precision (repos with ground truth):")
    print(f"   {'Repo':40s} {'TP':>5s} {'FP':>5s} {'Precision':>10s}")
    print(f"   {'-' * 40} {'-' * 5} {'-' * 5} {'-' * 10}")
    for repo, counts in sorted(repo_stats.items(), key=lambda x: -(x[1]["tp"] + x[1]["fp"])):
        total = counts["tp"] + counts["fp"]
        precision = counts["tp"] / total * 100 if total > 0 else 0
        print(f"   {repo:40s} {counts['tp']:5d} {counts['fp']:5d} {precision:>9.1f}%")

    # 4. Recommendations
    print("\n4. Recommendations:")
    all_fp_repos = [repo for repo, c in repo_stats.items() if c["tp"] == 0 and c["fp"] > 2]
    if all_fp_repos:
        print(f"   - Add to suppression list (100% FP): {', '.join(all_fp_repos)}")

    low_precision_strategies = [
        s for s, c in strategy_stats.items() if c["tp"] + c["fp"] > 3 and c["tp"] / (c["tp"] + c["fp"]) < 0.3
    ]
    if low_precision_strategies:
        print(f"   - Downweight strategies (<30% precision): {', '.join(low_precision_strategies)}")

    total_tp = sum(c["tp"] for c in repo_stats.values())
    total_fp = sum(c["fp"] for c in repo_stats.values())
    if total_tp + total_fp > 0:
        print(
            f"   - Overall precision: {total_tp / (total_tp + total_fp) * 100:.1f}% "
            f"({total_tp} TP / {total_tp + total_fp} total)"
        )
    print(
        f"   - Ground truth coverage: {total_tp + total_fp} / "
        f"{conn.execute('SELECT COUNT(*) FROM task_gaps').fetchone()[0]} total gaps"
    )


# ---------------------------------------------------------------------------
# --review: add a single manual review
# ---------------------------------------------------------------------------


def add_review(conn, ticket_id, repo, verdict, reason):
    ensure_table(conn)
    valid_verdicts = ("true_positive", "false_positive", "uncertain")
    if verdict not in valid_verdicts:
        print(f"Invalid verdict '{verdict}'. Must be one of: {valid_verdicts}")
        sys.exit(1)

    try:
        conn.execute(
            """INSERT INTO ground_truth (ticket_id, expected_repo, verdict, reason, reviewer)
               VALUES (?, ?, ?, ?, 'manual')""",
            (ticket_id, repo, verdict, reason),
        )
        conn.commit()
        print(f"Added: {ticket_id} / {repo} = {verdict}")
    except sqlite3.IntegrityError:
        conn.execute(
            """UPDATE ground_truth SET verdict = ?, reason = ?, reviewer = 'manual',
               reviewed_at = CURRENT_TIMESTAMP
               WHERE ticket_id = ? AND expected_repo = ?""",
            (verdict, reason, ticket_id, repo),
        )
        conn.commit()
        print(f"Updated: {ticket_id} / {repo} = {verdict}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Ground truth management for gap detection")
    parser.add_argument("--import-audit", action="store_true", help="Import audit findings")
    parser.add_argument("--stats", action="store_true", help="Show precision stats")
    parser.add_argument("--calibrate", action="store_true", help="Calibration report")
    parser.add_argument("--review", nargs=4, metavar=("TICKET", "REPO", "VERDICT", "REASON"), help="Add manual review")
    args = parser.parse_args()

    if not any([args.import_audit, args.stats, args.calibrate, args.review]):
        parser.print_help()
        sys.exit(1)

    conn = get_db()

    if args.import_audit:
        print("Importing audit findings...\n")
        import_audit(conn)

    if args.stats:
        show_stats(conn)

    if args.calibrate:
        calibrate(conn)

    if args.review:
        add_review(conn, *args.review)

    conn.close()


if __name__ == "__main__":
    main()
