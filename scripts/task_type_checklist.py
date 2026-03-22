#!/usr/bin/env python3
"""
Task-Type Checklist: infra repos that ALWAYS need changes for specific task types.

Based on statistical analysis of 104 real tasks:
- "New provider integration" (PI) tasks ALWAYS change certain repos
- These are NOT method consumers — they're infrastructure/config repos

Usage:
    python3 scripts/task_type_checklist.py --type provider-integration
    python3 scripts/task_type_checklist.py --type provider-integration --provider trustly
    python3 scripts/task_type_checklist.py --analyze   # discover patterns from data
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

_BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE_DIR / "db" / "knowledge.db"
if not DB_PATH.exists():
    DB_PATH = Path.home() / ".pay-knowledge" / "db" / "knowledge.db"

# Provider type mapping — derived from boilerplate-node-providers-grpc-service
PROVIDER_TYPE_MAP = {
    "card": "grpc-providers-{provider}",
    "apm": "grpc-apm-{provider}",
    "mpi": "grpc-mpi-{provider}",
}

# Standard boilerplate files every provider must have (from boilerplate analysis)
BOILERPLATE_FILES = {
    "methods/": "gRPC method handlers (sale, payout, refund, etc.)",
    "libs/payload-builders/": "API request payload builders per method",
    "libs/get-credentials.js": "Fetch provider API keys from credentials service",
    "libs/make-request.js": "HTTP client wrapper for provider API",
    "libs/map-response.js": "Map provider response to standard format",
    "libs/statuses-map.js": "Provider status code mapping",
    "env/consts.js": "Provider-specific constants",
}

# Standard webhook activity structure
WEBHOOK_ACTIVITY_FILES = {
    "activities/{provider}/handle-activities.js": "Main webhook activity router",
    "activities/{provider}/index.js": "Activity exports",
    "activities/{provider}/webhook/handle-activities.js": "Webhook-specific activities",
    "activities/{provider}/webhook/verify-signature.js": "Signature verification",
}

# Standard credential validation structure
CREDENTIAL_FILES = {
    "libs/{provider}/validation-strategy.js": "Credential field validation rules",
}


# Task type definitions based on statistical analysis of 104 tasks + boilerplate
TASK_TYPES = {
    "provider-integration": {
        "description": "New APM/provider integration or major provider feature",
        "detect_keywords": ["integration", "provider", "apm", "add support"],
        "detect_project": "PI",
        "always_repos": [
            ("grpc-providers-credentials", "Credential validation — libs/{provider}/validation-strategy.js", 19),
            ("workflow-provider-webhooks", "Webhook activities — activities/{provider}/ directory", 18),
            ("grpc-providers-features", "Feature flags — seeds.cql INSERT for provider", 16),
        ],
        "often_repos": [
            ("express-api-internal", "Internal API — process-initialize-data.js", 11),
            ("express-webhooks", "Webhook route — src/routes/provider/index.js (add route for provider)", 8),
            ("libs-types", "Proto common.proto updates (if new fields needed)", 7),
            ("express-api-callbacks", "Callback URL route — src/routes/{provider}-apm-callback.js", 6),
            ("express-api-v1", "API v1 — payment type consts", 5),
            ("grpc-core-schemas", "Schema updates for new payment type", 4),
            ("grpc-webhooks-paycom", "Webhook notification processing", 3),
            ("e2e-tests", "End-to-end test coverage", 2),
        ],
        "provider_specific": [
            ("grpc-apm-{provider}", "APM adapter — clone from boilerplate-node-providers-grpc-service"),
            ("grpc-providers-{provider}", "Card provider — clone from boilerplate-node-providers-grpc-service"),
        ],
    },
    "webhook-fix": {
        "description": "Fix or update webhook handling for existing provider",
        "detect_keywords": ["webhook", "expiration", "callback", "notification"],
        "detect_project": "PI",
        "always_repos": [
            ("workflow-provider-webhooks", "Webhook activity handlers", 18),
        ],
        "often_repos": [
            ("express-webhooks", "Webhook route (if new event type)", 8),
            ("grpc-webhooks-paycom", "Webhook notification processing", 4),
        ],
        "provider_specific": [
            ("grpc-apm-{provider}", "APM adapter (if response mapping changes)"),
            ("grpc-providers-{provider}", "Provider service (if API mapping changes)"),
        ],
    },
    "payment-method": {
        "description": "Add new payment method type or modify existing",
        "detect_keywords": ["payment method", "payout", "refund", "verification", "sale"],
        "detect_project": "PI",
        "always_repos": [
            ("grpc-providers-features", "Feature flags — enable method in seeds.cql", 16),
            ("grpc-providers-credentials", "Credential config for the method", 19),
        ],
        "often_repos": [
            ("grpc-payment-gateway", "Gateway routing — if new method type", 5),
            ("libs-types", "Proto updates for new request/response fields", 7),
        ],
        "provider_specific": [
            ("grpc-apm-{provider}", "Method handler implementation"),
            ("grpc-providers-{provider}", "Provider-level method implementation"),
        ],
    },
}


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def detect_task_type(summary: str, project: str) -> str | None:
    """Auto-detect task type from summary and project."""
    summary_lower = summary.lower()
    for type_name, config in TASK_TYPES.items():
        if config.get("detect_project") and project != config["detect_project"]:
            continue
        if any(kw in summary_lower for kw in config["detect_keywords"]):
            return type_name
    return None


def show_checklist(task_type: str, provider: str | None = None):
    """Show the checklist for a task type."""
    config = TASK_TYPES.get(task_type)
    if not config:
        print(f"Unknown task type: {task_type}")
        print(f"Available: {', '.join(TASK_TYPES.keys())}")
        return

    print(f"\n{'=' * 60}")
    print(f"  Checklist: {config['description']}")
    if provider:
        print(f"  Provider: {provider}")
    print(f"{'=' * 60}\n")

    print("✅ ALWAYS (these repos are changed in 50%+ of similar tasks):")
    for repo, desc, count in config["always_repos"]:
        print(f"  [ ] {repo}")
        print(f"      {desc} (changed in {count}/40 PI tasks)")

    print("\n📋 OFTEN (25-50% of similar tasks):")
    for repo, desc, count in config["often_repos"]:
        print(f"  [ ] {repo}")
        print(f"      {desc} (changed in {count}/40 PI tasks)")

    if provider and config.get("provider_specific"):
        print(f"\n🔧 PROVIDER-SPECIFIC ({provider}):")
        for template, desc in config["provider_specific"]:
            repo = template.replace("{provider}", provider)
            print(f"  [ ] {repo}")
            print(f"      {desc}")

    # Show boilerplate file structure for new provider
    if provider and task_type == "provider-integration":
        print(f"\n📦 BOILERPLATE FILES (standard structure for {provider}):")
        print("  Source: boilerplate-node-providers-grpc-service")
        for path, desc in BOILERPLATE_FILES.items():
            print(f"    {path:<40} {desc}")

        print("\n🔗 WEBHOOK ACTIVITIES (workflow-provider-webhooks):")
        for template, desc in WEBHOOK_ACTIVITY_FILES.items():
            path = template.replace("{provider}", provider)
            print(f"    {path:<55} {desc}")

        print("\n🔑 CREDENTIALS (grpc-providers-credentials):")
        for template, desc in CREDENTIAL_FILES.items():
            path = template.replace("{provider}", provider)
            print(f"    {path:<55} {desc}")

        print("\n⚙️  SEEDS.CQL (grpc-providers-features):")
        print(f"    Add rows for provider='{provider}' with supported operations")
        print("    Required fields: payment_method_type, verification, payout, etc.")

    print()


def analyze_patterns(conn: sqlite3.Connection):
    """Discover task type patterns from actual data."""
    print("=== Task Type Pattern Analysis ===\n")

    for project in ["PI", "CORE", "BO", "HS"]:
        rows = conn.execute(
            "SELECT ticket_id, repos_changed FROM task_history WHERE ticket_id LIKE ?",
            (f"{project}-%",),
        ).fetchall()

        if not rows:
            continue

        repo_counter: Counter = Counter()
        for row in rows:
            repos = json.loads(row["repos_changed"] or "[]")
            for r in repos:
                repo_counter[r] += 1

        total = len(rows)
        print(f"--- {project} ({total} tasks) ---")
        print(f"{'Repo':<45} {'Count':>5} {'%':>5}")
        for repo, count in repo_counter.most_common(10):
            pct = count * 100 // total
            bar = "█" * (pct // 5)
            print(f"  {repo:<43} {count:>5} {pct:>4}% {bar}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Task type infra checklist")
    parser.add_argument("--type", help="Task type (provider-integration, webhook-fix, payment-method)")
    parser.add_argument("--provider", help="Provider name (e.g., trustly, nuvei)")
    parser.add_argument("--detect", help="Auto-detect type from task summary")
    parser.add_argument("--analyze", action="store_true", help="Analyze patterns from data")
    args = parser.parse_args()

    if args.analyze:
        conn = get_db()
        analyze_patterns(conn)
        conn.close()
        return

    if args.detect:
        # Try to detect from summary
        project = args.detect.split("-")[0] if "-" in args.detect else ""
        conn = get_db()
        row = conn.execute("SELECT summary FROM task_history WHERE ticket_id = ?", (args.detect.upper(),)).fetchone()
        if row:
            detected = detect_task_type(row["summary"], project.upper())
            if detected:
                print(f"Detected type: {detected}")
                show_checklist(detected, args.provider)
            else:
                print(f"Could not detect task type from: {row['summary']}")
        else:
            print(f"Task {args.detect} not found in DB")
        conn.close()
        return

    if args.type:
        show_checklist(args.type, args.provider)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
