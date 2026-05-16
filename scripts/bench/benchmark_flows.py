#!/usr/bin/env python3
"""
Benchmark flow completeness — tests cross-service flow tracing.

Unlike benchmark_queries.py (single-query recall), this tests whether RAG
can reconstruct multi-hop chains across services. Each flow is an ordered
chain of (repo, file_pattern, must_contain) steps.

Flows are loaded from the active profile's benchmarks.yaml (flow_queries section).
If no profile benchmarks are found, the script exits with a helpful message.

Score = fraction of chain steps found per flow, averaged across all flows.

Usage:
    python3 scripts/benchmark_flows.py
    python3 scripts/benchmark_flows.py --verbose
"""

import json
import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))
from scripts._common import setup_paths

setup_paths()

from scripts.bench.bench_utils import (
    resolve_profile_dir,
)
from scripts.bench.bench_utils import (
    run_hybrid_search as _run_hybrid_search_base,
)

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE / "db" / "knowledge.db"
RESULTS_FILE = Path(__file__).parent.parent / "benchmark_flows_results.json"


def _load_flow_queries() -> list[dict]:
    """Load flow_queries from the active profile's benchmarks.yaml."""
    profile_dir = resolve_profile_dir()
    bench_path = profile_dir / "benchmarks.yaml"
    if not bench_path.exists():
        print(f"No benchmarks.yaml found in profile: {profile_dir}")
        print("Skipping benchmarks. To enable, create benchmarks.yaml based on profiles/example/benchmarks.yaml")
        sys.exit(0)  # Not an error — just skip
    data = yaml.safe_load(bench_path.read_text()) or {}
    queries = data.get("flow_queries", [])
    if not queries:
        print(f"No flow_queries defined in {bench_path}")
        print("Skipping benchmarks. To enable, add flow_queries entries to your profile's benchmarks.yaml")
        sys.exit(0)  # Not an error — just skip
    return queries


# ============================================================
# Flow definitions — loaded from active profile
# ============================================================

FLOWS = _load_flow_queries()


# ============================================================
# Evaluation functions
# ============================================================


def run_hybrid_search(query: str, limit: int = 15) -> dict:
    """Run full hybrid search pipeline, returning dict-style results for flow evaluation."""
    base = _run_hybrid_search_base(query, limit=limit)
    # Re-format results as dicts (flow evaluation expects repo/file/snippet keys)
    return {
        "repos": base["repos"],
        "results": [{"repo": repo, "file": fpath, "snippet": snippet} for repo, fpath, snippet in base["results"]],
        "error": base.get("error"),
    }


def run_trace_flow(source: str, target: str) -> dict:
    """Run trace_flow tool."""
    try:
        from src.graph.service import trace_flow_tool

        result = trace_flow_tool(source, target)
        return {"output": result, "found_path": "No path found" not in result}
    except Exception as e:
        return {"output": "", "found_path": False, "error": str(e)}


def evaluate_chain_step(step: dict, search_results: list[dict]) -> dict:
    """Evaluate whether a chain step is satisfied by search results.

    Checks two ways a repo can be "found":
    1. Direct: result has repo_name == target repo
    2. Mentioned: target repo name appears in any snippet text
       (e.g. flow annotations that reference target_repos)
    """
    repo = step["repo"]
    file_pattern = step.get("file_pattern")
    must_contain = step.get("must_contain", [])

    # Check if repo appears in results (direct match)
    repo_results = [r for r in search_results if r["repo"] == repo]
    repo_found = len(repo_results) > 0

    # Also check if repo is mentioned in any snippet (cross-reference)
    if not repo_found:
        all_snippets = " ".join(r.get("snippet", "") for r in search_results)
        repo_found = repo in all_snippets
        if repo_found:
            # Use all results for content checks since repo was found via mention
            repo_results = search_results

    # Check file pattern match (in file paths OR snippet content)
    file_found = True
    if file_pattern and repo_results:
        file_found = any(
            file_pattern in r.get("file", "") or file_pattern in r.get("snippet", "") for r in repo_results
        )

    # Check must_contain in snippets
    content_found = True
    missing_content = []
    if must_contain and repo_results:
        all_text = " ".join(r.get("snippet", "") for r in repo_results)
        for keyword in must_contain:
            if keyword.lower() not in all_text.lower():
                content_found = False
                missing_content.append(keyword)

    passed = repo_found and file_found and (content_found or not must_contain)

    return {
        "passed": passed,
        "repo_found": repo_found,
        "file_found": file_found,
        "content_found": content_found,
        "missing_content": missing_content,
        "matching_results": len(repo_results),
    }


def evaluate_flow(flow: dict, verbose: bool = False) -> dict:
    """Evaluate a single flow using all its search queries."""
    all_results: list[dict] = []
    query_details = []

    for query in flow["search_queries"]:
        sr = run_hybrid_search(query, limit=15)
        all_results.extend(sr["results"])
        query_details.append(
            {
                "query": query,
                "repos_found": sorted(sr["repos"]),
                "result_count": len(sr["results"]),
                "error": sr.get("error"),
            }
        )

    # Deduplicate results by (repo, file)
    seen = set()
    unique_results = []
    for r in all_results:
        key = (r["repo"], r["file"])
        if key not in seen:
            seen.add(key)
            unique_results.append(r)

    # Evaluate each chain step
    chain_results = []
    for step in flow["chain"]:
        result = evaluate_chain_step(step, unique_results)
        result["description"] = step["description"]
        chain_results.append(result)

    steps_passed = sum(1 for r in chain_results if r["passed"])
    total_steps = len(flow["chain"])
    score = steps_passed / total_steps if total_steps > 0 else 0.0

    # Check must_not_contain_repos
    confusion_detected = False
    if "must_not_contain_repos" in flow:
        all_repos = {r["repo"] for r in unique_results}
        bad_repos = set(flow["must_not_contain_repos"]) & all_repos
        if bad_repos:
            confusion_detected = True
            if verbose:
                print(f"  ⚠️  Confusion: found {bad_repos} in results")

    return {
        "id": flow["id"],
        "name": flow["name"],
        "score": score,
        "steps_passed": steps_passed,
        "total_steps": total_steps,
        "chain_results": chain_results,
        "query_details": query_details,
        "confusion_detected": confusion_detected,
    }


# ============================================================
# Main
# ============================================================


def main():
    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    profile_name = resolve_profile_dir().name
    print("=" * 70)
    print(f"Flow Completeness Benchmark ({len(FLOWS)} flows, profile: {profile_name})")
    print("=" * 70)

    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    start = time.time()
    results = []

    for flow in FLOWS:
        print(f"\n{flow['id']}: {flow['name']}")
        result = evaluate_flow(flow, verbose=verbose)
        results.append(result)

        status = "✅" if result["score"] == 1.0 else "⚠️" if result["score"] > 0 else "❌"
        print(f"  {status} Score: {result['score']:.0%} ({result['steps_passed']}/{result['total_steps']} steps)")

        if result["confusion_detected"]:
            print("  ⚠️  Repo confusion detected!")

        if verbose:
            for cr in result["chain_results"]:
                icon = "✓" if cr["passed"] else "✗"
                print(f"    [{icon}] {cr['description']}")
                if not cr["repo_found"]:
                    print("        repo not found in results")
                if not cr["file_found"]:
                    print("        file pattern not matched")
                if cr["missing_content"]:
                    print(f"        missing: {cr['missing_content']}")

            for qd in result["query_details"]:
                print(f"    Query: {qd['query']!r} → {qd['result_count']} results, repos: {qd['repos_found'][:5]}")

    elapsed = time.time() - start

    # Summary
    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)

    total_score = sum(r["score"] for r in results) / len(results)
    total_steps = sum(r["total_steps"] for r in results)
    total_passed = sum(r["steps_passed"] for r in results)
    confusions = sum(1 for r in results if r["confusion_detected"])

    print(f"Overall score: {total_score:.3f} ({total_passed}/{total_steps} steps)")
    print(f"Flows: {len(results)}")
    print(f"  Perfect (1.0): {sum(1 for r in results if r['score'] == 1.0)}")
    print(f"  Partial (>0):  {sum(1 for r in results if 0 < r['score'] < 1.0)}")
    print(f"  Failed (0):    {sum(1 for r in results if r['score'] == 0.0)}")
    print(f"  Confusions:    {confusions}")
    print(f"Time: {elapsed:.1f}s")

    # Save results
    output = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "overall_score": round(total_score, 4),
        "total_steps_passed": total_passed,
        "total_steps": total_steps,
        "confusions": confusions,
        "elapsed_seconds": round(elapsed, 1),
        "flows": [
            {
                "id": r["id"],
                "name": r["name"],
                "score": round(r["score"], 4),
                "steps_passed": r["steps_passed"],
                "total_steps": r["total_steps"],
                "confusion_detected": r["confusion_detected"],
            }
            for r in results
        ],
    }

    RESULTS_FILE.write_text(json.dumps(output, indent=2) + "\n")
    print(f"\nResults saved to {RESULTS_FILE.name}")


if __name__ == "__main__":
    main()
