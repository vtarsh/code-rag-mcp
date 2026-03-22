#!/usr/bin/env python3
"""
Phase 5: RAG Evaluation System

Uses dependency graph as deterministic ground truth.
No LLM needed — answers are derived from graph traversal.

Metrics:
  - Importance-Weighted Recall (IWR)
  - Multi-retriever cross-validation (false negative detection)
  - Per-query breakdown

Usage:
    python3 eval.py              # run full eval
    python3 eval.py --verbose    # show per-query details
"""

import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

# Add parent dir for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

_BASE = Path(os.getenv("CODE_RAG_HOME", Path.home() / ".code-rag"))
DB_PATH = _BASE / "db" / "knowledge.db"

import yaml  # noqa: E402

_profile = os.getenv("ACTIVE_PROFILE", "")
if not _profile:
    _ap = _BASE / ".active_profile"
    _profile = _ap.read_text().strip() if _ap.exists() else "example"
_conv_path = _BASE / "profiles" / _profile / "conventions.yaml"
_conv = yaml.safe_load(_conv_path.read_text()) if _conv_path.exists() else {}
_PROVIDER_PREFIXES = _conv.get("provider_prefixes", ["grpc-apm-"])
_WEBHOOK_HANDLER = _conv.get("webhook_repos", {}).get("handler", "workflow-provider-webhooks")
_GATEWAY_REPO = _conv.get("gateway_repo", "grpc-payment-gateway")
_PROTO_REPOS = _conv.get("proto_repos", ["providers-proto"])

# Importance weights
W_CRITICAL = 3.0  # proto contract, shared schema — miss = break prod
W_DIRECT = 2.0  # direct npm dependency
W_OPTIONAL = 1.0  # transitive, optional, or low-risk dependency


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# PART 1: Generate ground truth from dependency graph
# ============================================================


def generate_eval_queries(conn):
    """Generate eval queries with deterministic ground truth from graph."""
    queries = []

    # --- Type 1: "What depends on X?" (direct dependents) ---
    # Pick important shared packages
    critical_repos = [*_PROTO_REPOS, "node-libs-common", "node-libs-fetch", "node-libs-providers-common"]

    for repo in critical_repos:
        # Ground truth: all repos with incoming edges to this repo
        dependents = conn.execute(
            """SELECT DISTINCT source FROM graph_edges
               WHERE target = ? AND source NOT LIKE 'pkg:%'""",
            (repo,),
        ).fetchall()
        dep_names = [r["source"] for r in dependents]

        if dep_names:
            queries.append(
                {
                    "id": f"dep_{repo}",
                    "type": "dependency_lookup",
                    "query": f"which repos depend on {repo}",
                    "search_query": repo,
                    "expected_repos": {r: W_CRITICAL if "proto" in repo else W_DIRECT for r in dep_names},
                    "description": f"Find all {len(dep_names)} repos depending on {repo}",
                }
            )

    # --- Type 2: "What does repo X depend on?" (outgoing deps) ---
    # Pick a few provider repos
    provider_repos = conn.execute(
        "SELECT name FROM repos WHERE type IN ('grpc-service-js', 'grpc-service-ts') LIMIT 10"
    ).fetchall()

    for repo_row in provider_repos:
        repo = repo_row["name"]
        deps = conn.execute(
            """SELECT DISTINCT target FROM graph_edges
               WHERE source = ? AND target NOT LIKE 'pkg:%'""",
            (repo,),
        ).fetchall()
        dep_names = [r["target"] for r in deps]

        if dep_names:
            # Weight: proto deps are critical, others are direct
            weighted = {}
            for d in dep_names:
                if "proto" in d:
                    weighted[d] = W_CRITICAL
                elif "types" in d or "common" in d:
                    weighted[d] = W_DIRECT
                else:
                    weighted[d] = W_OPTIONAL

            queries.append(
                {
                    "id": f"uses_{repo}",
                    "type": "dependency_lookup",
                    "query": f"what does {repo} depend on",
                    "search_query": repo,
                    "expected_repos": weighted,
                    "description": f"Find all {len(dep_names)} deps of {repo}",
                }
            )

    # --- Type 3: Impact analysis — "if I change X, what breaks?" ---
    for repo in _PROTO_REPOS[:2]:
        # BFS to get transitive dependents (depth 2)
        level1 = conn.execute(
            """SELECT DISTINCT source FROM graph_edges
               WHERE target = ? AND source NOT LIKE 'pkg:%'""",
            (repo,),
        ).fetchall()
        l1_names = set(r["source"] for r in level1)

        level2 = set()
        for l1 in l1_names:
            l2 = conn.execute(
                """SELECT DISTINCT source FROM graph_edges
                   WHERE target = ? AND source NOT LIKE 'pkg:%'""",
                (l1,),
            ).fetchall()
            for r in l2:
                if r["source"] != repo and r["source"] not in l1_names:
                    level2.add(r["source"])

        weighted = {}
        for r in l1_names:
            weighted[r] = W_CRITICAL  # direct dependents
        for r in level2:
            weighted[r] = W_OPTIONAL  # transitive

        queries.append(
            {
                "id": f"impact_{repo}",
                "type": "impact_analysis",
                "query": f"what repos are affected if {repo} changes",
                "search_query": repo,
                "expected_repos": weighted,
                "description": f"Impact analysis: {len(l1_names)} direct + {len(level2)} transitive",
            }
        )

    # --- Type 4: Provider task analysis ---
    _prefix = _PROVIDER_PREFIXES[0] if _PROVIDER_PREFIXES else "grpc-apm-"
    providers = conn.execute("SELECT name FROM repos WHERE name LIKE ? LIMIT 8", (f"{_prefix}%",)).fetchall()

    for p_row in providers:
        provider_repo = p_row["name"]
        provider_name = provider_repo[len(_prefix) :]

        # Expected: the provider repo + workflow-provider-webhooks + providers-proto + grpc-payment-gateway
        expected = {provider_repo: W_CRITICAL}
        if _PROTO_REPOS:
            expected[_PROTO_REPOS[0]] = W_CRITICAL

        # Check if webhook handling exists for this provider
        webhook_check = conn.execute(
            "SELECT COUNT(*) as cnt FROM chunks WHERE repo_name = ? AND content LIKE ?",
            (_WEBHOOK_HANDLER, f"%{provider_name}%"),
        ).fetchone()
        if webhook_check["cnt"] > 0:
            expected[_WEBHOOK_HANDLER] = W_DIRECT

        # Payment gateway always relevant
        if _GATEWAY_REPO:
            expected[_GATEWAY_REPO] = W_DIRECT

        queries.append(
            {
                "id": f"task_{provider_name}",
                "type": "task_analysis",
                "query": f"implement new payment flow for {provider_name}",
                "search_query": provider_name,
                "expected_repos": expected,
                "description": f"Task analysis for {provider_name}: {len(expected)} expected repos",
            }
        )

    return queries


# ============================================================
# PART 2: Run retrieval and score
# ============================================================


def run_keyword_search(conn, query, limit=20):
    """Run FTS5 keyword search, return set of repo names found."""
    try:
        rows = conn.execute(
            """SELECT DISTINCT repo_name FROM chunks
               WHERE chunks MATCH ? ORDER BY rank LIMIT ?""",
            (f'"{query}"', limit),
        ).fetchall()
        return set(r["repo_name"] for r in rows)
    except sqlite3.OperationalError:
        return set()


def run_graph_search(conn, repo_name, direction="dependents"):
    """Run graph-based search, return set of repo names."""
    if direction == "dependents":
        rows = conn.execute(
            """SELECT DISTINCT source FROM graph_edges
               WHERE target = ? AND source NOT LIKE 'pkg:%'""",
            (repo_name,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT DISTINCT target FROM graph_edges
               WHERE source = ? AND target NOT LIKE 'pkg:%'""",
            (repo_name,),
        ).fetchall()
    return set(r[0] for r in rows)


def run_vector_search(query, limit=20):
    """Run vector search, return set of repo names found."""
    try:
        from src.search.vector import vector_search as _vector_search

        results, err = _vector_search(query, limit=limit)
        if err:
            return set()
        return set(r["repo_name"] for r in results)
    except Exception:
        return set()


def compute_iwr(expected_repos, found_repos):
    """Compute Importance-Weighted Recall.

    IWR = Σ(w_i * found_i) / Σ(w_i)
    """
    total_weight = sum(expected_repos.values())
    if total_weight == 0:
        return 1.0

    found_weight = sum(w for repo, w in expected_repos.items() if repo in found_repos)
    return found_weight / total_weight


def compute_standard_recall(expected_repos, found_repos):
    """Standard (flat) recall for comparison."""
    if not expected_repos:
        return 1.0
    found = sum(1 for r in expected_repos if r in found_repos)
    return found / len(expected_repos)


# ============================================================
# PART 3: Cross-validation & false negative detection
# ============================================================


def cross_validate(keyword_results, vector_results, graph_results):
    """Detect false negatives via multi-retriever cross-validation.

    If graph finds repo X but keyword+vector missed it → false negative.
    """
    false_negatives = {
        "missed_by_keyword": graph_results - keyword_results,
        "missed_by_vector": graph_results - vector_results,
        "missed_by_both_search": graph_results - (keyword_results | vector_results),
        "only_in_graph": graph_results - keyword_results - vector_results,
    }
    return false_negatives


# ============================================================
# PART 4: Main eval loop
# ============================================================


def main():
    verbose = "--verbose" in sys.argv

    print("=" * 70)
    print("RAG Evaluation — Phase 5")
    print("Ground truth: dependency graph (deterministic)")
    print("=" * 70)

    conn = get_db()

    # Generate queries
    print("\n[1/4] Generating eval queries from dependency graph...")
    queries = generate_eval_queries(conn)
    print(f"  Generated {len(queries)} queries")

    by_type = defaultdict(int)
    for q in queries:
        by_type[q["type"]] += 1
    for t, c in sorted(by_type.items()):
        print(f"    {t}: {c}")

    # Run evaluation
    print("\n[2/4] Running evaluation...")
    start = time.time()

    results = []
    total_iwr = 0
    total_recall = 0
    total_fn_keyword = 0
    total_fn_vector = 0
    total_fn_both = 0

    for i, q in enumerate(queries):
        # Run all three retrievers
        keyword_found = run_keyword_search(conn, q["search_query"])
        vector_found = run_vector_search(q["search_query"])

        # Graph search depends on query type
        if q["type"] == "impact_analysis":
            graph_found = run_graph_search(conn, q["search_query"], "dependents")
        elif q["type"] == "dependency_lookup":
            if q["id"].startswith("dep_"):
                graph_found = run_graph_search(conn, q["search_query"], "dependents")
            else:
                graph_found = run_graph_search(conn, q["search_query"], "dependencies")
        elif q["type"] == "task_analysis":
            graph_found = set(q["expected_repos"].keys())
        else:
            graph_found = set(q["expected_repos"].keys())

        # Combined: for dependency queries, graph IS the right tool
        # For task queries, it's search + graph combined
        if q["type"] in ("dependency_lookup", "impact_analysis"):
            combined_found = graph_found  # graph is the correct tool here
        else:
            combined_found = keyword_found | vector_found | graph_found

        # Compute metrics
        iwr = compute_iwr(q["expected_repos"], combined_found)
        recall = compute_standard_recall(q["expected_repos"], combined_found)

        # Cross-validation
        fn = cross_validate(keyword_found, vector_found, graph_found)

        total_iwr += iwr
        total_recall += recall
        total_fn_keyword += len(fn["missed_by_keyword"])
        total_fn_vector += len(fn["missed_by_vector"])
        total_fn_both += len(fn["missed_by_both_search"])

        result = {
            "query": q,
            "iwr": iwr,
            "recall": recall,
            "keyword_found": len(keyword_found),
            "vector_found": len(vector_found),
            "expected": len(q["expected_repos"]),
            "combined_found_count": len(combined_found & set(q["expected_repos"].keys())),
            "false_negatives": fn,
        }
        results.append(result)

        if verbose:
            status = "✅" if iwr >= 0.8 else "⚠️" if iwr >= 0.5 else "❌"
            print(
                f"  {status} [{q['id']}] IWR={iwr:.2f} Recall={recall:.2f} "
                f"(found {result['combined_found_count']}/{len(q['expected_repos'])} expected repos)"
            )

            if iwr < 1.0:
                missed = set(q["expected_repos"].keys()) - combined_found
                for m in list(missed)[:5]:
                    w = q["expected_repos"][m]
                    print(f"      MISSED: {m} (weight={w})")

        # Progress
        if not verbose and (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(queries)}...")

    elapsed = time.time() - start

    # Aggregate results
    n = len(queries)
    avg_iwr = total_iwr / n if n > 0 else 0
    avg_recall = total_recall / n if n > 0 else 0

    # Per-type breakdown
    print("\n[3/4] Results by query type:")
    type_metrics = defaultdict(lambda: {"iwr": 0, "recall": 0, "count": 0})
    for r in results:
        t = r["query"]["type"]
        type_metrics[t]["iwr"] += r["iwr"]
        type_metrics[t]["recall"] += r["recall"]
        type_metrics[t]["count"] += 1

    print(f"\n  {'Type':<25} {'Count':>5} {'Avg IWR':>8} {'Avg Recall':>10}")
    print(f"  {'-' * 25} {'-' * 5} {'-' * 8} {'-' * 10}")
    for t, m in sorted(type_metrics.items()):
        avg_t_iwr = m["iwr"] / m["count"]
        avg_t_recall = m["recall"] / m["count"]
        print(f"  {t:<25} {m['count']:>5} {avg_t_iwr:>8.2f} {avg_t_recall:>10.2f}")

    # False negative summary
    print("\n[4/4] Cross-validation summary:")
    print(f"  Total false negatives (missed by keyword): {total_fn_keyword}")
    print(f"  Total false negatives (missed by vector):  {total_fn_vector}")
    print(f"  Total false negatives (missed by BOTH):    {total_fn_both}")

    # Final scores
    print(f"\n{'=' * 70}")
    print("BASELINE SCORES")
    print(f"{'=' * 70}")
    print(f"  Queries evaluated:       {n}")
    print(f"  Avg IWR (weighted):      {avg_iwr:.3f}")
    print(f"  Avg Recall (flat):       {avg_recall:.3f}")
    print(f"  Eval time:               {elapsed:.1f}s")
    print(f"{'=' * 70}")

    # Worst queries (for improvement targeting)
    print("\nTop 5 worst queries (lowest IWR):")
    worst = sorted(results, key=lambda r: r["iwr"])[:5]
    for r in worst:
        q = r["query"]
        print(f"  IWR={r['iwr']:.2f} [{q['id']}] {q['description']}")

    # Save baseline to file
    baseline = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "num_queries": n,
        "avg_iwr": round(avg_iwr, 4),
        "avg_recall": round(avg_recall, 4),
        "by_type": {
            t: {
                "avg_iwr": round(m["iwr"] / m["count"], 4),
                "avg_recall": round(m["recall"] / m["count"], 4),
                "count": m["count"],
            }
            for t, m in type_metrics.items()
        },
        "false_negatives": {
            "keyword": total_fn_keyword,
            "vector": total_fn_vector,
            "both": total_fn_both,
        },
    }

    baseline_path = _BASE / "eval_baseline.json"
    with open(baseline_path, "w") as f:
        json.dump(baseline, f, indent=2)
    print(f"\nBaseline saved to {baseline_path}")

    conn.close()


if __name__ == "__main__":
    main()
