#!/usr/bin/env python3
"""
Benchmark conceptual queries — tests semantic understanding, not just graph traversal.

Queries are loaded from the active profile's benchmarks.yaml (conceptual_queries section).
If no profile benchmarks are found, the script exits with a helpful message.

Usage:
    python3 scripts/benchmark_queries.py
    python3 scripts/benchmark_queries.py --verbose
"""

import json
import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.bench.bench_utils import (
    get_db,
    resolve_profile_dir,
    run_fts_search,
    run_hybrid_search,
)

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))


def _load_benchmark_queries() -> list[dict]:
    """Load conceptual_queries from the active profile's benchmarks.yaml."""
    profile_dir = resolve_profile_dir()
    bench_path = profile_dir / "benchmarks.yaml"
    if not bench_path.exists():
        print(f"No benchmarks.yaml found in profile: {profile_dir}")
        print("Skipping benchmarks. To enable, create benchmarks.yaml based on profiles/example/benchmarks.yaml")
        sys.exit(0)  # Not an error — just skip
    data = yaml.safe_load(bench_path.read_text()) or {}
    queries = data.get("conceptual_queries", [])
    if not queries:
        print(f"No conceptual_queries defined in {bench_path}")
        print("Skipping benchmarks. To enable, add conceptual_queries entries to your profile's benchmarks.yaml")
        sys.exit(0)  # Not an error — just skip
    return queries


# ============================================================
# Benchmark query definitions — loaded from active profile
# ============================================================

BENCHMARK_QUERIES = _load_benchmark_queries()


# ============================================================
# Retrieval functions
# ============================================================


def run_vector_search(query, limit=20):
    """Run vector search, return set of repo names."""
    try:
        from src.search.vector import vector_search as _vector_search

        results, err = _vector_search(query, limit=limit)
        if err:
            return {"repos": set(), "results": [], "error": err}
        return {
            "repos": set(r["repo_name"] for r in results),
            "results": [(r["repo_name"], r["file_path"], r.get("content_preview", "")[:200]) for r in results],
        }
    except Exception as e:
        return {"repos": set(), "results": [], "error": str(e)}


def run_graph_dependents(conn, repo_name):
    """Get all repos that depend on repo_name."""
    rows = conn.execute(
        """SELECT DISTINCT source, edge_type FROM graph_edges
           WHERE target = ? AND source NOT LIKE 'pkg:%'
           AND source NOT LIKE 'proto:%' AND source NOT LIKE 'msg:%'
           AND source NOT LIKE 'svc:%'""",
        (repo_name,),
    ).fetchall()
    return {r["source"]: r["edge_type"] for r in rows}


def run_graph_dependencies(conn, repo_name):
    """Get all repos that repo_name depends on."""
    rows = conn.execute(
        """SELECT DISTINCT target, edge_type FROM graph_edges
           WHERE source = ? AND target NOT LIKE 'pkg:%'
           AND target NOT LIKE 'proto:%' AND target NOT LIKE 'msg:%'
           AND target NOT LIKE 'svc:%'""",
        (repo_name,),
    ).fetchall()
    return {r["target"]: r["edge_type"] for r in rows}


def check_keyword_in_results(results_list, keywords):
    """Check which keywords appear in result snippets."""
    found = {}
    for kw in keywords:
        kw_lower = kw.lower()
        count = sum(1 for _, _, snippet in results_list if kw_lower in snippet.lower())
        found[kw] = count
    return found


# ============================================================
# Main benchmark
# ============================================================


def main():
    verbose = "--verbose" in sys.argv

    profile_name = resolve_profile_dir().name
    print("=" * 70)
    print(f"Benchmark: {len(BENCHMARK_QUERIES)} Conceptual Queries (profile: {profile_name})")
    print("Testing search quality for real-world questions")
    print("=" * 70)

    conn = get_db()
    start = time.time()
    results = []

    for bq in BENCHMARK_QUERIES:
        print(f"\n{'─' * 60}")
        print(f"[{bq['id']}] {bq['question']}")
        print(f"  Note: {bq['note']}")

        qresult = {
            "id": bq["id"],
            "question": bq["question"],
            "scores": {},
        }

        # --- Test each search query variant ---
        all_found_repos = set()
        all_results = []

        for sq in bq["search_queries"]:
            # FTS
            fts = run_fts_search(conn, sq)
            all_found_repos |= fts["repos"]
            all_results.extend(fts["results"])

            # Hybrid (includes vector + reranker)
            hybrid = run_hybrid_search(sq)
            all_found_repos |= hybrid["repos"]
            all_results.extend(hybrid["results"])

            if verbose:
                print(f"\n  Query: '{sq}'")
                fts_display = ", ".join(sorted(fts["repos"])[:8])
                hybrid_display = ", ".join(sorted(hybrid["repos"])[:8])
                print(f"    FTS repos ({len(fts['repos'])}): {fts_display}")
                print(f"    Hybrid repos ({len(hybrid['repos'])}): {hybrid_display}")

        # --- Graph checks ---
        graph_score = None
        if bq.get("expected_via_graph"):
            repo = bq["search_queries"][0]
            dependents = run_graph_dependents(conn, repo)
            graph_count = len(dependents)
            min_expected = bq.get("expected_min_dependents", 0)
            graph_score = 1.0 if graph_count >= min_expected else graph_count / max(min_expected, 1)
            all_found_repos |= set(dependents.keys())
            if verbose:
                print(f"    Graph dependents: {graph_count} (expected >= {min_expected})")
            qresult["scores"]["graph"] = {
                "count": graph_count,
                "min_expected": min_expected,
                "pass": graph_count >= min_expected,
            }

        if bq.get("expected_outgoing_types"):
            repo = "workflow-provider-webhooks"
            deps = run_graph_dependencies(conn, repo)
            found_types = set(deps.values())
            expected_types = set(bq["expected_outgoing_types"])
            type_recall = len(found_types & expected_types) / len(expected_types) if expected_types else 1.0
            if verbose:
                print(f"    Outgoing edge types: {found_types & expected_types}")
                outgoing_repos = [r for r, t in deps.items() if t in expected_types]
                print(f"    Connected repos: {', '.join(sorted(outgoing_repos)[:10])}")
            qresult["scores"]["graph_types"] = {"found": list(found_types & expected_types), "recall": type_recall}

        # --- Score: expected repos found ---
        expected = bq.get("expected_repos", {})
        if expected:
            found_weight = sum(w for r, w in expected.items() if r in all_found_repos)
            total_weight = sum(expected.values())
            repo_recall = found_weight / total_weight if total_weight else 1.0

            missed = {r: w for r, w in expected.items() if r not in all_found_repos}
            qresult["scores"]["repo_recall"] = repo_recall
            qresult["scores"]["missed_repos"] = missed

            status = "PASS" if repo_recall >= 0.8 else "PARTIAL" if repo_recall >= 0.5 else "FAIL"
            icon = {"PASS": "✅", "PARTIAL": "⚠️", "FAIL": "❌"}[status]
            print(
                f"  {icon} Repo recall: {repo_recall:.2f} ({len(expected) - len(missed)}/{len(expected)} expected repos found)"
            )

            if missed and verbose:
                for r, w in missed.items():
                    print(f"    MISSED: {r} (weight={w})")
        else:
            qresult["scores"]["repo_recall"] = 1.0

        # --- Score: keyword presence in snippets ---
        expected_kw = bq.get("expected_keywords", [])
        if expected_kw:
            kw_found = check_keyword_in_results(all_results, expected_kw)
            kw_recall = sum(1 for v in kw_found.values() if v > 0) / len(expected_kw)
            qresult["scores"]["keyword_recall"] = kw_recall
            if verbose:
                for kw, count in kw_found.items():
                    status = "✓" if count > 0 else "✗"
                    print(f"    Keyword '{kw}': {status} ({count} hits)")
        else:
            qresult["scores"]["keyword_recall"] = 1.0

        # --- Composite score ---
        scores = [qresult["scores"].get("repo_recall", 1.0), qresult["scores"].get("keyword_recall", 1.0)]
        if graph_score is not None:
            scores.append(graph_score)
        qresult["composite"] = sum(scores) / len(scores)

        results.append(qresult)

    elapsed = time.time() - start
    conn.close()

    # ============================================================
    # Summary
    # ============================================================
    print(f"\n{'=' * 70}")
    print("BENCHMARK RESULTS")
    print(f"{'=' * 70}")
    print(f"\n{'ID':<5} {'Score':>6} {'Repo':>6} {'KW':>6} {'Question'}")
    print(f"{'─' * 5} {'─' * 6} {'─' * 6} {'─' * 6} {'─' * 40}")

    total_composite = 0
    for r in results:
        repo_r = r["scores"].get("repo_recall", 1.0)
        kw_r = r["scores"].get("keyword_recall", 1.0)
        print(f"{r['id']:<5} {r['composite']:>5.2f}  {repo_r:>5.2f}  {kw_r:>5.2f}  {r['question'][:40]}")
        total_composite += r["composite"]

    avg = total_composite / len(results) if results else 0
    print(f"\n{'Average composite score':.<40} {avg:.3f}")
    print(f"{'Eval time':.<40} {elapsed:.1f}s")
    print(f"{'=' * 70}")

    # Pass/fail
    passing = sum(1 for r in results if r["composite"] >= 0.8)
    print(f"\n{passing}/{len(results)} queries PASS (>= 0.80)")

    if avg < 0.8:
        print("\n⚠️  Search quality below threshold. Consider:")
        print("  - Tuning FTS5/vector weights in _hybrid_search()")
        print("  - Adding domain terms to DOMAIN_GLOSSARY")
        print("  - Improving chunking for missed content types")

    # Save results
    output_path = _BASE / "benchmark_results.json"
    with open(output_path, "w") as f:
        json.dump(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "avg_composite": round(avg, 4),
                "passing": passing,
                "total": len(results),
                "queries": results,
            },
            f,
            indent=2,
            default=str,
        )
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()
