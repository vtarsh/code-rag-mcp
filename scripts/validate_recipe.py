#!/usr/bin/env python3
"""Validate recipes against ground truth from task_history.

Checks whether a recipe's repo predictions cover the repos that were
actually changed in completed tasks. Reports coverage, precision, and
missed repos.

Usage:
    python scripts/validate_recipe.py
    python scripts/validate_recipe.py --task PI-60
    python scripts/validate_recipe.py --recipe new_apm_provider
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "profiles" / "pay-com"
DB_PATH = ROOT / "db" / "knowledge.db"


def load_recipes():
    path = PROFILE / "recipes.yaml"
    if not path.exists():
        print(f"ERROR: {path} not found")
        sys.exit(1)
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("recipes", {})


def get_task_repos(conn, task_ids):
    """Get repos_changed for given tasks."""
    results = {}
    for tid in task_ids:
        row = conn.execute(
            "SELECT repos_changed FROM task_history WHERE ticket_id = ?", (tid,)
        ).fetchone()
        if row and row[0]:
            results[tid] = set(json.loads(row[0]))
        else:
            results[tid] = set()
    return results


def extract_recipe_repos(recipe, provider=None):
    """Extract all repos from a recipe, expanding {provider} templates."""
    repos = {"core": set(), "common": set(), "conditional": set()}

    for tier in ["core", "common", "conditional"]:
        for entry in recipe.get("repos", {}).get(tier, []):
            repo = entry["repo"]
            # Split "repo1 OR repo2" syntax into separate alternatives
            alternatives = [r.strip() for r in repo.split(" OR ")]
            for alt in alternatives:
                r = alt
                if provider and "{provider}" in r:
                    r = r.replace("{provider}", provider)
                # Skip entries with unexpanded template placeholders ({domain}, {all}, etc.)
                # These are documentation notes, not literal repo predictions.
                if "{" in r and "}" in r:
                    continue
                repos[tier].add(r)

    return repos


def detect_provider(task_repos):
    """Detect provider name from grpc-apm-* or grpc-providers-* repo."""
    for repo in task_repos:
        if repo.startswith("grpc-apm-"):
            return repo.replace("grpc-apm-", "")
    for repo in task_repos:
        if repo.startswith("grpc-providers-") and repo not in ("grpc-providers-credentials", "grpc-providers-features", "grpc-providers-proto"):
            return repo.replace("grpc-providers-", "")
    return None


def validate_task(recipe, task_id, actual_repos, provider):
    """Validate recipe against one task's actual repos."""
    recipe_repos = extract_recipe_repos(recipe, provider)
    all_recipe = recipe_repos["core"] | recipe_repos["common"] | recipe_repos["conditional"]
    core_common = recipe_repos["core"] | recipe_repos["common"]

    covered = actual_repos & all_recipe
    missed = actual_repos - all_recipe
    extra = all_recipe - actual_repos

    # Core+common coverage (these are the repos recipe says you'll definitely need)
    core_covered = actual_repos & core_common
    core_missed = actual_repos - core_common

    recall_all = len(covered) / len(actual_repos) * 100 if actual_repos else 0
    recall_core = len(core_covered) / len(actual_repos) * 100 if actual_repos else 0
    precision = len(covered) / len(all_recipe) * 100 if all_recipe else 0

    return {
        "task_id": task_id,
        "provider": provider,
        "actual_repos": sorted(actual_repos),
        "actual_count": len(actual_repos),
        "covered": sorted(covered),
        "missed": sorted(missed),
        "extra_predicted": sorted(extra),
        "recall_all_tiers": round(recall_all, 1),
        "recall_core_common": round(recall_core, 1),
        "precision": round(precision, 1),
        "core_repos_predicted": sorted(core_common),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", help="Validate single task (e.g. PI-60)")
    parser.add_argument("--recipe", default="new_apm_provider", help="Recipe name")
    args = parser.parse_args()

    recipes = load_recipes()
    if args.recipe not in recipes:
        print(f"Recipe '{args.recipe}' not found. Available: {list(recipes.keys())}")
        sys.exit(1)

    recipe = recipes[args.recipe]
    evidence_tasks = recipe.get("evidence", {}).get("sample_tasks", [])

    conn = sqlite3.connect(str(DB_PATH))

    evidence = recipe.get("evidence", {})
    edge_case_tasks = evidence.get("edge_case_tasks", [])

    if args.task:
        task_ids = [args.task]
    else:
        task_ids = evidence_tasks

    all_task_ids = task_ids + [t for t in edge_case_tasks if t not in task_ids]
    task_repos = get_task_repos(conn, all_task_ids)

    print(f"=== Recipe Validation: {args.recipe} ===\n")

    all_recalls = []
    all_precisions = []

    def _print_task(tid, label=""):
        actual = task_repos.get(tid, set())
        if not actual:
            print(f"  {tid}: no data in task_history, skipping")
            return None

        provider = detect_provider(actual)
        result = validate_task(recipe, tid, actual, provider)

        suffix = f" [{label}]" if label else ""
        print(f"--- {tid} ({provider}, {result['actual_count']} repos){suffix} ---")
        print(f"  Recall (all tiers):    {result['recall_all_tiers']}%")
        print(f"  Recall (core+common):  {result['recall_core_common']}%")
        print(f"  Precision:             {result['precision']}%")

        if result["covered"]:
            print(f"  Covered:  {', '.join(result['covered'])}")
        if result["missed"]:
            print(f"  MISSED:   {', '.join(result['missed'])}")
        if result["extra_predicted"]:
            print(f"  Extra:    {', '.join(result['extra_predicted'])}")
        print()
        return result

    for tid in task_ids:
        result = _print_task(tid)
        if result:
            all_recalls.append(result["recall_all_tiers"])
            all_precisions.append(result["precision"])

    if edge_case_tasks and not args.task:
        print("=== EDGE CASES ===\n")
        for tid in edge_case_tasks:
            _print_task(tid, label="edge case")

    if all_recalls:
        print("=== AGGREGATE ===")
        avg_recall = sum(all_recalls) / len(all_recalls)
        avg_precision = sum(all_precisions) / len(all_precisions)
        print(f"  Avg recall (all tiers): {avg_recall:.1f}%")
        print(f"  Avg precision:          {avg_precision:.1f}%")
        print(f"  Tasks validated:        {len(all_recalls)}")

    conn.close()


if __name__ == "__main__":
    main()
