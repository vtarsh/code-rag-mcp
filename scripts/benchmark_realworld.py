#!/usr/bin/env python3
"""
Real-world benchmark: developer queries loaded from the active profile.

Queries are loaded from the active profile's benchmarks.yaml (realworld_queries section).
If no profile benchmarks are found, the script exits with a helpful message.

Usage:
    python3 scripts/benchmark_realworld.py
    python3 scripts/benchmark_realworld.py --verbose
    python3 scripts/benchmark_realworld.py --verbose --query=RW03
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE / "db" / "knowledge.db"


def _resolve_profile_dir() -> Path:
    """Resolve the active profile directory (mirrors src/config.py logic)."""
    if env_profile := os.getenv("ACTIVE_PROFILE"):
        return _BASE / "profiles" / env_profile
    marker = _BASE / ".active_profile"
    if marker.exists():
        name = marker.read_text().strip()
        if name and (_BASE / "profiles" / name).is_dir():
            return _BASE / "profiles" / name
    return _BASE / "profiles" / "example"


def _load_realworld_queries() -> list[dict]:
    """Load realworld_queries from the active profile's benchmarks.yaml."""
    profile_dir = _resolve_profile_dir()
    bench_path = profile_dir / "benchmarks.yaml"
    if not bench_path.exists():
        print(f"No benchmarks.yaml found in profile: {profile_dir}")
        print("Skipping benchmarks. To enable, create benchmarks.yaml based on profiles/example/benchmarks.yaml")
        sys.exit(0)  # Not an error — just skip
    data = yaml.safe_load(bench_path.read_text()) or {}
    queries = data.get("realworld_queries", [])
    if not queries:
        print(f"No realworld_queries defined in {bench_path}")
        print("Skipping benchmarks. To enable, add realworld_queries entries to your profile's benchmarks.yaml")
        sys.exit(0)  # Not an error — just skip
    return queries


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# Real-world queries — loaded from active profile
# ============================================================

REALWORLD_QUERIES = _load_realworld_queries()


# ============================================================
# Retrieval functions (same as benchmark_queries.py)
# ============================================================


def run_fts_search(conn, query, limit=20):
    tokens = query.split()
    sanitized = []
    for t in tokens:
        if len(t) < 3:
            continue
        if "-" in t:
            sanitized.append(f'"{t}"')
        else:
            sanitized.append(t)
    fts_query = " OR ".join(sanitized) if sanitized else query

    try:
        rows = conn.execute(
            """SELECT repo_name, file_path,
                      snippet(chunks, 0, '>>>', '<<<', '...', 30) as snippet
               FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?""",
            (fts_query, limit),
        ).fetchall()
        return {
            "repos": set(r["repo_name"] for r in rows),
            "results": [(r["repo_name"], r["file_path"], r["snippet"][:200]) for r in rows],
        }
    except sqlite3.OperationalError as e:
        return {"repos": set(), "results": [], "error": str(e)}


def run_hybrid_search(query, limit=10):
    try:
        from src.search.fts import expand_query
        from src.search.hybrid import hybrid_search

        expanded = expand_query(query)
        ranked, err, _total = hybrid_search(expanded, limit=limit)
        return {
            "repos": set(r["repo_name"] for r in ranked),
            "results": [(r["repo_name"], r["file_path"], r.get("snippet", "")[:200]) for r in ranked],
            "error": err,
        }
    except Exception as e:
        return {"repos": set(), "results": [], "error": str(e)}


def run_graph_dependents(conn, repo_name):
    rows = conn.execute(
        """SELECT DISTINCT source, edge_type FROM graph_edges
           WHERE target = ? AND source NOT LIKE 'pkg:%'
           AND source NOT LIKE 'proto:%' AND source NOT LIKE 'msg:%'
           AND source NOT LIKE 'svc:%'""",
        (repo_name,),
    ).fetchall()
    return {r["source"]: r["edge_type"] for r in rows}


def check_keyword_in_results(results_list, keywords):
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
    filter_query = None
    for arg in sys.argv:
        if arg.startswith("--query="):
            filter_query = arg.split("=", 1)[1].upper()

    profile_name = _resolve_profile_dir().name
    print("=" * 70)
    print(f"Real-World Benchmark: {len(REALWORLD_QUERIES)} Developer Queries (profile: {profile_name})")
    print("Testing search quality for daily development tasks")
    print("=" * 70)

    conn = get_db()
    start = time.time()
    results = []
    categories = {}

    queries = REALWORLD_QUERIES
    if filter_query:
        queries = [q for q in queries if q["id"] == filter_query]
        if not queries:
            print(f"Query {filter_query} not found")
            return

    for bq in queries:
        print(f"\n{'─' * 60}")
        print(f"[{bq['id']}] {bq['question']}")
        print(f"  Category: {bq['category']}")

        qresult = {
            "id": bq["id"],
            "question": bq["question"],
            "category": bq["category"],
            "scores": {},
        }

        # --- Run searches ---
        all_found_repos = set()
        all_results = []

        for sq in bq["search_queries"]:
            fts = run_fts_search(conn, sq)
            all_found_repos |= fts["repos"]
            all_results.extend(fts["results"])

            hybrid = run_hybrid_search(sq)
            all_found_repos |= hybrid["repos"]
            all_results.extend(hybrid["results"])

            if verbose:
                print(f"\n  Query: '{sq}'")
                fts_display = ", ".join(sorted(fts["repos"])[:8])
                hybrid_display = ", ".join(sorted(hybrid["repos"])[:8])
                print(f"    FTS repos ({len(fts['repos'])}): {fts_display}")
                print(f"    Hybrid repos ({len(hybrid['repos'])}): {hybrid_display}")
                if fts.get("error"):
                    print(f"    FTS error: {fts['error']}")
                if hybrid.get("error"):
                    print(f"    Hybrid error: {hybrid['error']}")

        # --- Graph checks ---
        graph_score = None
        if bq.get("expected_via_graph"):
            graph_repo = bq.get("graph_repo", bq["search_queries"][0])
            dependents = run_graph_dependents(conn, graph_repo)
            graph_count = len(dependents)
            min_expected = bq.get("expected_min_dependents", 0)
            graph_score = 1.0 if graph_count >= min_expected else graph_count / max(min_expected, 1)
            all_found_repos |= set(dependents.keys())
            if verbose:
                print(f"    Graph dependents of {graph_repo}: {graph_count} (expected >= {min_expected})")
            qresult["scores"]["graph"] = {
                "repo": graph_repo,
                "count": graph_count,
                "min_expected": min_expected,
                "pass": graph_count >= min_expected,
            }

        # --- Score: expected repos found ---
        expected = bq.get("expected_repos", {})
        if expected:
            found_weight = sum(w for r, w in expected.items() if r in all_found_repos)
            total_weight = sum(expected.values())
            repo_recall = found_weight / total_weight if total_weight else 1.0

            missed = {r: w for r, w in expected.items() if r not in all_found_repos}
            qresult["scores"]["repo_recall"] = repo_recall
            qresult["scores"]["missed_repos"] = missed
            qresult["scores"]["found_repos"] = [r for r in expected if r in all_found_repos]

            status = "PASS" if repo_recall >= 0.8 else "PARTIAL" if repo_recall >= 0.5 else "FAIL"
            icon = {"PASS": "\u2705", "PARTIAL": "\u26a0\ufe0f", "FAIL": "\u274c"}[status]
            print(
                f"  {icon} Repo recall: {repo_recall:.2f} ({len(expected) - len(missed)}/{len(expected)} expected repos found)"
            )

            if missed and verbose:
                for r, w in missed.items():
                    print(f"    MISSED: {r} (weight={w})")
        else:
            # No specific repos expected — check minimum result count
            min_results = bq.get("expected_min_results", 1)
            has_enough = len(all_found_repos) >= min_results
            qresult["scores"]["repo_recall"] = 1.0 if has_enough else 0.5
            qresult["scores"]["total_repos_found"] = len(all_found_repos)
            icon = "\u2705" if has_enough else "\u26a0\ufe0f"
            print(f"  {icon} Found {len(all_found_repos)} repos (expected >= {min_results})")

        # --- Score: keyword presence ---
        expected_kw = bq.get("expected_keywords", [])
        if expected_kw:
            kw_found = check_keyword_in_results(all_results, expected_kw)
            kw_recall = sum(1 for v in kw_found.values() if v > 0) / len(expected_kw)
            qresult["scores"]["keyword_recall"] = kw_recall
            qresult["scores"]["keyword_details"] = {k: v for k, v in kw_found.items()}
            if verbose:
                for kw, count in kw_found.items():
                    s = "\u2713" if count > 0 else "\u2717"
                    print(f"    Keyword '{kw}': {s} ({count} hits)")
        else:
            qresult["scores"]["keyword_recall"] = 1.0

        # --- Composite score ---
        scores = [
            qresult["scores"].get("repo_recall", 1.0),
            qresult["scores"].get("keyword_recall", 1.0),
        ]
        if graph_score is not None:
            scores.append(graph_score)
        qresult["composite"] = sum(scores) / len(scores)

        # Track by category
        cat = bq["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(qresult["composite"])

        results.append(qresult)

    elapsed = time.time() - start
    conn.close()

    # ============================================================
    # Summary
    # ============================================================
    print(f"\n{'=' * 70}")
    print("REAL-WORLD BENCHMARK RESULTS")
    print(f"{'=' * 70}")
    print(f"\n{'ID':<6} {'Score':>6} {'Repo':>6} {'KW':>6} {'Cat':<14} {'Question'}")
    print(f"{'─' * 6} {'─' * 6} {'─' * 6} {'─' * 6} {'─' * 14} {'─' * 35}")

    total_composite = 0
    for r in results:
        repo_r = r["scores"].get("repo_recall", 1.0)
        kw_r = r["scores"].get("keyword_recall", 1.0)
        print(
            f"{r['id']:<6} {r['composite']:>5.2f}  {repo_r:>5.2f}  {kw_r:>5.2f}  {r['category']:<14} {r['question'][:35]}"
        )
        total_composite += r["composite"]

    avg = total_composite / len(results) if results else 0

    # Category breakdown
    print(f"\n{'─' * 50}")
    print("Category scores:")
    for cat, scores in sorted(categories.items()):
        cat_avg = sum(scores) / len(scores)
        print(f"  {cat:<14} {cat_avg:.3f}  ({len(scores)} queries)")

    print(f"\n{'Average composite score':.<40} {avg:.3f}")
    print(f"{'Eval time':.<40} {elapsed:.1f}s")
    print(f"{'=' * 70}")

    passing = sum(1 for r in results if r["composite"] >= 0.8)
    partial = sum(1 for r in results if 0.5 <= r["composite"] < 0.8)
    failing = sum(1 for r in results if r["composite"] < 0.5)
    print(f"\n{passing} PASS / {partial} PARTIAL / {failing} FAIL")

    if avg < 0.8:
        print("\nWeakest queries:")
        for r in sorted(results, key=lambda x: x["composite"])[:3]:
            print(f"  {r['id']}: {r['composite']:.2f} — {r['question']}")

    # Save results
    output_path = _BASE / "benchmark_realworld_results.json"
    with open(output_path, "w") as f:
        json.dump(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "type": "realworld",
                "avg_composite": round(avg, 4),
                "passing": passing,
                "partial": partial,
                "failing": failing,
                "total": len(results),
                "categories": {cat: round(sum(s) / len(s), 4) for cat, s in categories.items()},
                "queries": results,
            },
            f,
            indent=2,
            default=str,
        )
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()
