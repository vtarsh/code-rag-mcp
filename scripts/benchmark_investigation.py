#!/usr/bin/env python3
"""Benchmark investigation quality: run analyze_task on test prompts and compare to ground truth.

Sends prompts to daemon HTTP API (POST /tool/analyze_task) with NO hints.
Compares repos found against ground truth from test_ground_truth.yaml.

Usage:
    python scripts/benchmark_investigation.py
    python scripts/benchmark_investigation.py --verbose

Requires daemon running on :8742.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import yaml

BASE_DIR = Path(os.getenv("CODE_RAG_HOME", Path(__file__).resolve().parent.parent))
PROFILE = os.getenv("ACTIVE_PROFILE", "pay-com")
PROFILE_DIR = BASE_DIR / "profiles" / PROFILE
GROUND_TRUTH_PATH = PROFILE_DIR / "test_ground_truth.yaml"
DAEMON_URL = "http://localhost:8742"

VERBOSE = "--verbose" in sys.argv


def load_ground_truth() -> dict:
    """Load test prompts and expected repos."""
    if not GROUND_TRUTH_PATH.exists():
        print(f"Ground truth not found: {GROUND_TRUTH_PATH}")
        sys.exit(1)
    return yaml.safe_load(GROUND_TRUTH_PATH.read_text())


def call_analyze_task(description: str) -> str:
    """Call analyze_task via daemon HTTP API."""
    payload = json.dumps({"description": description}).encode()
    req = urllib.request.Request(
        f"{DAEMON_URL}/tool/analyze_task",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data.get("result", "")
    except Exception as e:
        return f"ERROR: {e}"


def extract_repos_from_output(output: str) -> set[str]:
    """Extract repo names from analyze_task markdown output.

    Looks for **bold repo names** which is the analyze_task convention.
    Also picks up repo names from structured sections.
    """
    repos = set()
    # Pattern: **repo-name** (bold in markdown)
    for m in re.finditer(r"\*\*([a-z][a-z0-9-]+(?:-[a-z0-9]+)*)\*\*", output):
        name = m.group(1)
        # Filter out non-repo bold text (short words, common markdown bold)
        if len(name) >= 5 and "-" in name:
            repos.add(name)

    # Also catch repo names in bullet points like "- grpc-apm-payper"
    for m in re.finditer(r"[-•]\s*(grpc-[a-z0-9-]+|workflow-[a-z0-9-]+|express-[a-z0-9-]+|providers-[a-z0-9-]+)", output):
        repos.add(m.group(1))

    return repos


def main():
    gt = load_ground_truth()
    prompts = gt.get("prompts", {})

    print("=" * 70)
    print("Investigation Benchmark")
    print("=" * 70)

    total_expected = 0
    total_found = 0
    results = []

    for name, prompt_data in prompts.items():
        text = prompt_data["text"].strip()
        expected = set(prompt_data["expected_repos"])
        task_id = prompt_data.get("task_id", "?")

        print(f"\n--- {name} (ground truth: {task_id}) ---")
        print(f"  Prompt: {text[:100]}...")
        print(f"  Expected: {len(expected)} repos")

        output = call_analyze_task(text)

        if output.startswith("ERROR:"):
            print(f"  {output}")
            results.append({"prompt": name, "score": 0, "found": 0, "expected": len(expected)})
            total_expected += len(expected)
            continue

        found_repos = extract_repos_from_output(output)
        matched = expected & found_repos
        missed = expected - found_repos
        extra = found_repos - expected

        score = len(matched) / len(expected) if expected else 1.0
        total_found += len(matched)
        total_expected += len(expected)

        print(f"  Found: {len(found_repos)} repos total, {len(matched)}/{len(expected)} expected")
        print(f"  Score: {score:.0%}")

        if missed:
            print(f"  MISSED: {', '.join(sorted(missed))}")
        if extra and VERBOSE:
            print(f"  Extra: {', '.join(sorted(extra))}")

        results.append({
            "prompt": name,
            "task_id": task_id,
            "score": score,
            "found": len(matched),
            "expected": len(expected),
            "missed": sorted(missed),
            "extra": sorted(extra),
        })

        if VERBOSE:
            # Save raw output
            out_path = PROFILE_DIR / f"investigation_{name.lower()}.txt"
            out_path.write_text(output)
            print(f"  Raw output saved to {out_path}")

    # Summary
    overall = total_found / total_expected if total_expected > 0 else 0
    print(f"\n{'=' * 70}")
    print(f"Overall: {total_found}/{total_expected} repos found ({overall:.0%})")
    print(f"{'=' * 70}")

    print(f"\n{'Prompt':<12} {'Task':<8} {'Score':<8} {'Found':<8} {'Expected':<10}")
    print("-" * 46)
    for r in results:
        print(f"{r['prompt']:<12} {r.get('task_id','?'):<8} {r['score']:<8.0%} {r['found']:<8} {r['expected']:<10}")

    # Save results
    results_path = PROFILE_DIR / "investigation_results.json"
    with open(results_path, "w") as f:
        json.dump({"overall_score": overall, "results": results}, f, indent=2)
    print(f"\nResults saved to {results_path}")


if __name__ == "__main__":
    main()
